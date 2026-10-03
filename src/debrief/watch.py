"""``debrief serve --watch``: keep evidence current without being asked.

A polling thread (standard library only, so it works on every host) watches
each registered project's records, branches, worktrees and default branch.
When records or refs change it re-ingests the affected features, maps new
default-branch commits to features (squash landings), and syncs project
archives with their remotes every ``pull_interval`` seconds.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

from . import archive, gitutil, ingest, paths, projects, squash

POLL_SECONDS = 5.0
DIRTY_CHECK_SECONDS = 30.0


# Written by the viewer itself (or merged in by sync, which is noticed on its own): a change to only
# these isn't news for the viewer that made it.
VIEWER_FILES = ("comments.json", "feedback.md")


def records_signature(feature_dir: Path, agent_only: bool = False) -> Tuple[int, int]:
    """(newest mtime, file count) of a feature's records, ignoring evidence.

    ``agent_only`` leaves out what the viewer writes (comments, queued
    feedback, legs it marks for closing), so the viewer's own writes don't
    announce themselves as new records.
    """
    newest = 0
    count = 0
    for dirpath, dirnames, filenames in os.walk(feature_dir):
        dirnames[:] = [d for d in dirnames if d not in ("evidence", "squash") and not (agent_only and d == "legs")]
        for name in filenames:
            if name.startswith(".tmp-") or name.endswith(".lock"):
                continue
            if agent_only and name in VIEWER_FILES:
                continue
            try:
                stat = os.stat(os.path.join(dirpath, name))
            except OSError:
                continue
            newest = max(newest, stat.st_mtime_ns)
            count += 1
    return newest, count


class Watcher:
    def __init__(self, app=None, cfg=None, root: Optional[Path] = None):
        self.app = app
        self.root = root or paths.archive_root()
        self.pull_interval = cfg.get_int("sync", "pull_interval", 120) if cfg else 120
        self.records: Dict[str, Tuple[int, int]] = {}
        self.agent_records: Dict[str, Tuple[int, int]] = {}
        self.refs: Dict[str, Dict[str, str]] = {}
        self.dirty: Dict[str, bool] = {}
        self.last_dirty_check: Dict[str, float] = {}
        self.last_sync: Dict[str, float] = {}
        self.stop_event = threading.Event()

    def _changed_features(self, pid: str):
        """Features whose records changed, and whether any change came from an agent rather than the viewer."""
        base = paths.project_dir(pid, self.root) / "features"
        if not base.is_dir():
            return [], False
        changed, news = [], False
        for feature_dir in sorted(p for p in base.iterdir() if p.is_dir()):
            key = f"{pid}/{feature_dir.name}"
            sig = records_signature(feature_dir)
            if self.records.get(key) != sig:
                self.records[key] = sig
                changed.append(feature_dir.name)
                agent = records_signature(feature_dir, agent_only=True)
                if self.agent_records.get(key) != agent:
                    news = news or key in self.agent_records
                    self.agent_records[key] = agent
        return changed, news

    def tick(self) -> int:
        """One polling pass. Returns the number of features ingested.

        The viewer's change counter moves only for news: new evidence, an
        agent's records, or records another host pushed. The viewer's own
        writes already counted themselves.
        """
        ingested = 0
        news = False
        now = time.monotonic()
        for pid in projects.list_projects(self.root):
            project_dir = paths.project_dir(pid, self.root)
            if archive.remote_url(project_dir) and now - self.last_sync.get(pid, 0) >= self.pull_interval:
                self.last_sync[pid] = now
                try:
                    result = archive.sync(project_dir, "Sync records")
                    news = news or bool(result.get("incoming"))  # another host's records arrived
                except Exception as exc:  # keep watching; report once per failure
                    print(f"debrief: syncing {pid} failed: {exc}", file=sys.stderr)
            changed, agent_news = self._changed_features(pid)
            news = news or agent_news
            features = set(changed)
            repo = projects.repo_workdir(pid, self.root)
            if repo is not None:
                refs = gitutil.refs(repo)
                if refs != self.refs.get(pid):
                    first = pid not in self.refs
                    self.refs[pid] = refs
                    if not first:
                        features.update(ingest.feature_ids(pid, self.root))
                        try:
                            squash.follow_default_branch(pid, self.root)
                        except Exception as exc:
                            print(f"debrief: following {pid}'s default branch failed: {exc}", file=sys.stderr)
                if now - self.last_dirty_check.get(pid, 0) >= DIRTY_CHECK_SECONDS:
                    self.last_dirty_check[pid] = now
                    dirty = any(gitutil.status_porcelain(Path(t["path"])) for t in gitutil.worktrees(repo)
                                if not t.get("bare") and Path(t["path"]).exists())
                    if dirty or self.dirty.get(pid):
                        features.update(f for f in ingest.feature_ids(pid, self.root) if self._has_open_leg(pid, f))
                    self.dirty[pid] = dirty
            for fid in sorted(features):
                evidence = paths.feature_dir(pid, fid, self.root) / "evidence" / "evidence.json"
                before = _stamp(evidence)
                try:
                    ingest.ingest_feature(pid, fid, self.root)
                    ingested += 1
                except Exception as exc:
                    print(f"debrief: ingest of {pid}/{fid} failed: {exc}", file=sys.stderr)
                news = news or _stamp(evidence) != before  # ingest leaves unchanged evidence untouched
                fdir = paths.feature_dir(pid, fid, self.root)
                self.records[f"{pid}/{fid}"] = records_signature(fdir)
                self.agent_records[f"{pid}/{fid}"] = records_signature(fdir, agent_only=True)
        if news and self.app is not None and hasattr(self.app, "generation"):
            self.app.generation += 1
        return ingested

    def _has_open_leg(self, pid: str, fid: str) -> bool:
        legs_dir = paths.feature_dir(pid, fid, self.root) / "legs"
        if not legs_dir.is_dir():
            return False
        from . import records

        legs = records.load_legs(paths.feature_dir(pid, fid, self.root))
        return bool(legs) and not legs[-1].get("closed_at")

    def run(self) -> None:
        # The first pass records the current state; serve's startup ingest covers it.
        for pid in projects.list_projects(self.root):
            self._changed_features(pid)
            repo = projects.repo_workdir(pid, self.root)
            if repo is not None:
                self.refs[pid] = gitutil.refs(repo)
        while not self.stop_event.wait(POLL_SECONDS):
            try:
                self.tick()
            except Exception as exc:  # never let the watcher die
                print(f"debrief: watcher pass failed: {exc}", file=sys.stderr)


def _stamp(path: Path) -> Optional[int]:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def start(app=None, cfg=None, root: Optional[Path] = None) -> Watcher:
    watcher = Watcher(app, cfg, root)
    threading.Thread(target=watcher.run, name="debrief-watch", daemon=True).start()
    return watcher

"""Archive repositories: one git repository per project inside the archive.

These are the only repositories Debrief writes to. Commits disable signing
(the archive is tool-managed and must never block on a passphrase prompt),
and pulls never fail on conflicts: Debrief merges what it understands and
keeps both versions of anything it doesn't, flagging the feature for review.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional

from . import gitutil, residency, util
from .config import Config, load as load_config

GITIGNORE = """\
# Written by Debrief. Host-local state never syncs.
.tmp-*
*.lock
.DS_Store
"""


def _identity_env(project_dir: Path) -> Dict[str, str]:
    name = gitutil.try_run(["config", "user.name"], project_dir)
    email = gitutil.try_run(["config", "user.email"], project_dir)
    env: Dict[str, str] = {}
    host = util.hostname()
    if not name:
        env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = f"Debrief ({host})"
    if not email:
        env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = f"debrief@{host}.invalid"
    return env


def _git(project_dir: Path, args: List[str], check: bool = True, ok_codes=(0,)) -> str:
    return gitutil.run(
        ["-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", *args],
        project_dir,
        check=check,
        env_extra=_identity_env(project_dir),
        ok_codes=ok_codes,
    )


def is_repo(project_dir: Path) -> bool:
    return (Path(project_dir) / ".git").exists()


def ensure_repo(project_dir: Path) -> None:
    project_dir = Path(project_dir)
    project_dir.mkdir(parents=True, exist_ok=True)
    if is_repo(project_dir):
        return
    _git(project_dir, ["init", "-q", "-b", "main"])
    util.write_text(project_dir / ".gitignore", GITIGNORE)


def lock(project_dir: Path):
    return util.file_lock(Path(project_dir) / ".git" / "debrief.lock")


def commit(project_dir: Path, message: str, paths: Optional[List[str]] = None) -> Optional[str]:
    """Stage ``paths`` (or everything) and commit. Returns the sha, or None if clean."""
    project_dir = Path(project_dir)
    ensure_repo(project_dir)
    _git(project_dir, ["add", "-A", "--", *(paths or ["."])])
    staged = gitutil.run(["diff", "--cached", "--name-only"], project_dir).strip()
    if not staged:
        return None
    _git(project_dir, ["commit", "-q", "-m", message])
    return gitutil.head(project_dir)


def remote_url(project_dir: Path) -> Optional[str]:
    return gitutil.try_run(["config", "--get", "remote.origin.url"], project_dir) or None


def set_remote(project_dir: Path, url: str, cfg: Optional[Config] = None) -> None:
    residency.check_url(url, "Archive remote", cfg)
    if remote_url(project_dir):
        _git(project_dir, ["remote", "set-url", "origin", url])
    else:
        _git(project_dir, ["remote", "add", "origin", url])


def _branch(project_dir: Path) -> str:
    return gitutil.current_branch(project_dir) or "main"


def _remote_has_branch(project_dir: Path, branch: str) -> bool:
    out = gitutil.try_run(["ls-remote", "--heads", "origin", branch], project_dir)
    return bool(out)


def _merge_json_records(path: str, ours: Optional[bytes], theirs: Optional[bytes]) -> Optional[bytes]:
    """Merge the JSON records Debrief understands. None means "keep both"."""
    try:
        a = json.loads(ours.decode("utf-8")) if ours else None
        b = json.loads(theirs.decode("utf-8")) if theirs else None
    except (ValueError, UnicodeDecodeError):
        return None
    if a is None or b is None:
        merged = a if b is None else b
        return (json.dumps(merged, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    name = Path(path).name
    if name == "comments.json" and isinstance(a, dict) and isinstance(b, dict):
        by_id: Dict[str, dict] = {}
        for item in (a.get("comments") or []) + (b.get("comments") or []):
            cid = item.get("id")
            if not cid:
                continue
            prev = by_id.get(cid)
            if prev is None or (item.get("updated_at") or "") >= (prev.get("updated_at") or ""):
                by_id[cid] = item
        merged = dict(a)
        merged["comments"] = sorted(by_id.values(), key=lambda c: c.get("created_at") or "")
        return (json.dumps(merged, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    if "/legs/" in "/" + path and isinstance(a, dict) and isinstance(b, dict):
        merged = dict(b)
        merged.update({k: v for k, v in a.items() if v not in (None, [], "")})
        for key in ("sessions", "commits"):
            seen, combined = set(), []
            for item in (a.get(key) or []) + (b.get(key) or []):
                marker = json.dumps(item, sort_keys=True)
                if marker not in seen:
                    seen.add(marker)
                    combined.append(item)
            merged[key] = combined
        for key in ("closed_at", "close_requested_at"):
            values = [v for v in (a.get(key), b.get(key)) if v]
            merged[key] = max(values) if values else None
        return (json.dumps(merged, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    return None


def _resolve_conflicts(project_dir: Path) -> List[str]:
    """Resolve an in-progress merge. Returns files kept in both versions."""
    conflicted = gitutil.run(["diff", "--name-only", "--diff-filter=U"], project_dir).split()
    kept_both: List[str] = []
    stamp = util.now_iso().replace(":", "")
    for rel in conflicted:
        ours = gitutil.file_at(project_dir, ":2", rel)  # stage 2: our side of the merge
        theirs = gitutil.file_at(project_dir, ":3", rel)  # stage 3: the remote side
        target = project_dir / rel
        merged = _merge_json_records(rel, ours, theirs) if rel.endswith(".json") else None
        if merged is not None:
            util.write_bytes(target, merged)
        else:
            if ours is not None:
                util.write_bytes(target, ours)
            if theirs is not None:
                util.write_bytes(target.with_name(f"{target.name}.conflict-{stamp}"), theirs)
                kept_both.append(rel)
        _git(project_dir, ["add", "-A", "--", rel])
    if kept_both:
        record = project_dir / "conflicts.json"
        existing = util.read_json(record, {"conflicts": []})
        for rel in kept_both:
            existing["conflicts"].append({"path": rel, "at": util.now_iso(), "host": util.hostname()})
        util.write_json(record, existing)
        _git(project_dir, ["add", "conflicts.json"])
    _git(project_dir, ["commit", "-q", "--no-edit", "-m", "Merge remote records" + (
        f" (kept both versions of {util.human_count(len(kept_both), 'file')})" if kept_both else "")])
    return kept_both


def pull(project_dir: Path, cfg: Optional[Config] = None) -> dict:
    project_dir = Path(project_dir)
    url = remote_url(project_dir)
    if not url:
        return {"pulled": False, "reason": "no remote"}
    residency.check_url(url, "Archive remote", cfg)
    branch = _branch(project_dir)
    with lock(project_dir):
        try:
            _git(project_dir, ["fetch", "-q", "origin"])
        except gitutil.GitError as exc:
            return {"pulled": False, "reason": exc.stderr or str(exc)}
        remote_ref = f"refs/remotes/origin/{branch}"
        if not gitutil.try_run(["rev-parse", "--verify", "-q", remote_ref], project_dir):
            return {"pulled": False, "reason": "remote has no records yet"}
        if not gitutil.head(project_dir):
            _git(project_dir, ["reset", "-q", "--hard", remote_ref])
            return {"pulled": True, "conflicts": []}
        _git(project_dir, ["merge", "--no-edit", "-q", remote_ref], check=False)
        unmerged = gitutil.run(["diff", "--name-only", "--diff-filter=U"], project_dir).split()
        if unmerged:
            kept = _resolve_conflicts(project_dir)
            return {"pulled": True, "conflicts": kept}
        return {"pulled": True, "conflicts": []}


def push(project_dir: Path, cfg: Optional[Config] = None) -> dict:
    project_dir = Path(project_dir)
    url = remote_url(project_dir)
    if not url:
        return {"pushed": False, "reason": "no remote"}
    residency.check_url(url, "Archive remote", cfg)
    branch = _branch(project_dir)
    with lock(project_dir):
        try:
            _git(project_dir, ["push", "-q", "origin", f"HEAD:refs/heads/{branch}"])
        except gitutil.GitError as exc:
            return {"pushed": False, "reason": exc.stderr or str(exc)}
    return {"pushed": True}


def sync(project_dir: Path, message: str = "Sync records", cfg: Optional[Config] = None) -> dict:
    """Commit local changes, pull (merging), and push."""
    cfg = cfg or load_config()
    with lock(project_dir):
        committed = commit(project_dir, f"{message} from {util.hostname()}")
    result = {"committed": committed}
    result.update(pull(project_dir, cfg))
    if remote_url(project_dir):
        # A merge may have created a commit; push whatever we have now.
        result.update(push(project_dir, cfg))
    return result

"""Ingest: check what agents claim against what git shows.

For each feature this snapshots the patch and the base and head versions of
every changed file, indexes every commit by SHA, resolves anchors, and
computes coverage, flags, test evidence and the review queue. It writes only
to the feature's ``evidence/`` directory, works in place and is idempotent.
Project repositories are only read.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from . import __version__, diffparse, gitutil, paths, projects, records, rules, symbols, util

EVIDENCE_VERSION = 1
MAX_BLOB_BYTES = 1_000_000
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}


def git_blob_id(data: bytes) -> str:
    """The id git gives a blob with this content (SHA-1 repositories)."""
    return hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest()


class BlobReader:
    """Reads objects through one ``git cat-file --batch`` process."""

    def __init__(self, repo: Path):
        self.repo = repo
        self.proc: Optional[subprocess.Popen] = None

    def _start(self) -> None:
        self.proc = subprocess.Popen(
            ["git", "cat-file", "--batch"], cwd=str(self.repo), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=gitutil._env(),
        )

    def read(self, spec: str) -> Optional[bytes]:
        if not spec or "\n" in spec:
            return None
        if self.proc is None:
            self._start()
        assert self.proc and self.proc.stdin and self.proc.stdout
        try:
            self.proc.stdin.write(spec.encode("utf-8", "surrogateescape") + b"\n")
            self.proc.stdin.flush()
            header = self.proc.stdout.readline().decode("utf-8", "replace").strip()
        except (BrokenPipeError, OSError):
            self.close()
            return None
        parts = header.split()
        if len(parts) != 3 or parts[1] == "missing":
            return None
        size = int(parts[2])
        data = self.proc.stdout.read(size)
        self.proc.stdout.read(1)
        return data if parts[1] == "blob" else None

    def close(self) -> None:
        if self.proc is not None:
            try:
                if self.proc.stdin:
                    self.proc.stdin.close()
                self.proc.wait(timeout=5)
            except Exception:  # pragma: no cover
                self.proc.kill()
            finally:
                if self.proc.stdout:
                    self.proc.stdout.close()
            self.proc = None


def _decode(data: Optional[bytes]) -> Optional[str]:
    if data is None or b"\x00" in data[:8000]:
        return None
    return data.decode("utf-8", "replace")


class FeatureIngest:
    """One ingest run for one feature."""

    def __init__(self, project_id: str, feature_id: str, root: Optional[Path] = None, repo: Optional[Path] = None):
        self.project_id = project_id
        self.feature_id = feature_id
        self.root = root or paths.archive_root()
        self.project_dir = paths.project_dir(project_id, self.root)
        self.feature_dir = paths.feature_dir(project_id, feature_id, self.root)
        self.evidence_dir = self.feature_dir / "evidence"
        self.repo = Path(repo) if repo else None
        self.blobs: Optional[BlobReader] = None
        self.worktree: Optional[Path] = None
        self._texts: Dict[str, Optional[str]] = {}
        self._symbols: Dict[str, List[symbols.Symbol]] = {}
        self._stored_blobs: Set[str] = set()

    # --- locating code ---------------------------------------------------------

    def find_repo(self, feature: dict) -> Optional[Path]:
        candidates: List[Path] = []
        if self.repo:
            candidates.append(self.repo)
        workdir = projects.repo_workdir(self.project_id, self.root)
        if workdir:
            candidates.append(workdir)
        for sess in reversed(feature["sessions"]):
            root = sess["meta"].get("repo_root")
            if root:
                candidates.append(Path(root))
        for path in candidates:
            if path.exists() and gitutil.common_dir(path):
                return path
        return None

    def object_exists(self, sha: Optional[str]) -> bool:
        return bool(sha) and sha != "WORKTREE" and gitutil.rev_parse(self.repo, sha) is not None

    def text_at(self, rev: str, path: str) -> Optional[str]:
        """File content at a commit, or in the worktree when rev is WORKTREE."""
        key = f"{rev}:{path}"
        if key not in self._texts:
            if rev == "WORKTREE" and self.worktree is not None:
                try:
                    data = (self.worktree / path).read_bytes()
                except OSError:
                    data = None
            else:
                data = self.blobs.read(key) if self.blobs else None
            self._texts[key] = _decode(data)
        return self._texts[key]

    def text_of_blob(self, blob: Optional[str]) -> Optional[str]:
        if not blob:
            return None
        key = f"blob:{blob}"
        if key not in self._texts:
            data = self._read_blob(blob)
            self._texts[key] = _decode(data)
        return self._texts[key]

    def _read_blob(self, blob: str) -> Optional[bytes]:
        stored = self.evidence_dir / "blobs" / blob
        if stored.exists():
            return stored.read_bytes()
        return self.blobs.read(blob) if self.blobs else None

    def store_blob(self, blob: Optional[str], data: Optional[bytes] = None) -> Optional[str]:
        """Archive a file version for diff context; returns the blob id stored."""
        if not blob or blob in self._stored_blobs:
            return blob
        target = self.evidence_dir / "blobs" / blob
        if not target.exists():
            if data is None:
                data = self.blobs.read(blob) if self.blobs else None
            if data is None or len(data) > MAX_BLOB_BYTES:
                return None
            util.write_bytes(target, data)
        self._stored_blobs.add(blob)
        return blob

    def symbols_at_head(self, path: str) -> List[symbols.Symbol]:
        if path not in self._symbols:
            text = self.text_at(self.head_rev, path)
            self._symbols[path] = symbols.index(path, text) if text is not None else []
        return self._symbols[path]

    # --- the run ------------------------------------------------------------------

    def run(self) -> dict:
        feature = records.load_feature(self.feature_dir)
        legs = feature["legs"]
        previous = util.read_json(self.evidence_dir / "evidence.json", None)
        self.repo = self.find_repo(feature)
        if not legs:
            return self._write(self._skeleton(feature, note="No legs yet: no session has started."))
        if self.repo is None:
            if isinstance(previous, dict):
                previous["issues"] = records.all_issues(feature)
                previous["repo_available"] = False
                return previous
            return self._write(self._skeleton(feature, note="The repository is not on this host."))
        self.blobs = BlobReader(self.repo)
        try:
            return self._write(self._compute(feature))
        finally:
            self.blobs.close()

    def _skeleton(self, feature: dict, note: str) -> dict:
        return {
            "evidence_version": EVIDENCE_VERSION,
            "computed_at": util.now_iso(),
            "debrief_version": __version__,
            "project_id": self.project_id,
            "feature_id": self.feature_id,
            "repo_available": False,
            "note": note,
            "legs": [], "commits": [], "files": [], "anchors": [], "tests": [], "critical_paths": [],
            "coverage": {"units": 0, "covered": 0, "incidental": 0, "weak": 0, "unclaimed": 0, "ratio": None},
            "queue": [], "issues": records.all_issues(feature), "stats": {},
        }

    def _write(self, evidence: dict) -> dict:
        util.write_json(self.evidence_dir / "evidence.json", evidence)
        return evidence

    def _feature_head(self, feature: dict) -> Tuple[Optional[str], List[str]]:
        """(head commit, pending commits after the last closed leg)."""
        legs = feature["legs"]
        last = legs[-1]
        branch = last.get("branch")
        tip = gitutil.rev_parse(self.repo, f"refs/heads/{branch}") if branch else None
        if last.get("closed_at"):
            closed_head = last.get("head_commit") or last.get("head_ref")
            if closed_head == "WORKTREE":
                closed_head = None
            if tip and closed_head and tip != closed_head and gitutil.is_ancestor(self.repo, closed_head, tip):
                return tip, gitutil.rev_list(self.repo, f"{closed_head}..{tip}")
            if closed_head and self.object_exists(closed_head):
                return closed_head, []
            return tip, []
        if tip:
            return tip, []
        for sess in reversed(feature["sessions"]):
            for key in ("head_commit", "last_seen_head", "base_ref"):
                sha = sess["meta"].get(key)
                if sha and self.object_exists(sha):
                    return sha, []
        return last.get("base_ref"), []

    def _dirty_worktree(self, branch: Optional[str]) -> Optional[Path]:
        if not branch:
            return None
        for tree in gitutil.worktrees(self.repo):
            if tree.get("branch") == branch and not tree.get("bare"):
                path = Path(tree["path"])
                if path.exists() and gitutil.status_porcelain(path):
                    return path
        return None

    def _effective_base(self, base: str, head: str) -> str:
        """Drop upstream changes merged into the branch: use the newest merge-base."""
        ref = gitutil.default_branch_ref(self.repo)
        if not ref:
            return base
        mb = gitutil.merge_base(self.repo, ref, head)
        if mb and mb != head and mb != base and gitutil.is_ancestor(self.repo, base, mb):
            return mb
        return base

    def _diff(self, base: str, head: str, binary: bool = True) -> str:
        args = ["diff", *gitutil.DIFF_ARGS]
        if binary:
            args.append("--binary")
        if head == "WORKTREE":
            text = gitutil.run(args + [base], self.worktree)
            for path in gitutil.untracked(self.worktree):
                text += gitutil.run(["diff", "--no-index", *gitutil.DIFF_ARGS, "--binary", "--", "/dev/null", path],
                                    self.worktree, ok_codes=(0, 1))
            return text
        return gitutil.run(args + [base, head], self.repo)

    def _compute(self, feature: dict) -> dict:
        legs = feature["legs"]
        head_commit, pending = self._feature_head(feature)
        base = legs[0].get("base_ref") or head_commit
        branch = legs[-1].get("branch")
        self.worktree = self._dirty_worktree(branch) if not legs[-1].get("closed_at") else None
        self.head_rev = "WORKTREE" if self.worktree else head_commit
        eff_base = self._effective_base(base, head_commit) if base and head_commit else base
        logged_shas, logged_pids = self._logged(legs)

        # Feature patch and files -------------------------------------------------------------
        patch_text = self._diff(eff_base, self.head_rev) if eff_base and self.head_rev else ""
        patch_name = f"{eff_base}..{self.head_rev}.patch"
        util.write_text(self.evidence_dir / patch_name, patch_text)
        files = diffparse.parse(patch_text)
        self._store_file_versions(files)
        feature_patch_id = gitutil.patch_id(self.repo, patch_text) if self.head_rev != "WORKTREE" else None

        # Anchors ------------------------------------------------------------------------------
        anchors = self._resolve_anchors(feature, eff_base)
        brief_meta = (feature.get("brief") or {}).get("meta", {})
        incidental = brief_meta.get("incidental", [])
        rule_set = rules.load(self.project_dir)
        tests_by_target = self._tests_by_target(feature)

        # Coverage, flags ---------------------------------------------------------------------
        file_entries, flags, units = [], [], []
        for fp in files:
            entry, file_flags, file_units = self._file_evidence(fp, anchors, incidental, rule_set, tests_by_target)
            file_entries.append(entry)
            flags.extend(file_flags)
            units.extend(file_units)
        coverage = self._coverage(units)

        # Tests --------------------------------------------------------------------------------
        test_evidence = self._test_evidence(feature, head_commit)
        cp_status = self._critical_path_status(feature, test_evidence)

        # Commits and legs ---------------------------------------------------------------------------
        leg_entries, commit_entries = self._commits_and_legs(feature, head_commit, pending, logged_shas, logged_pids,
                                                              anchors)

        queue = self._queue(feature, file_entries, flags, anchors, test_evidence, cp_status, commit_entries)
        sessions = feature["sessions"]
        stats = {
            "files": len(files),
            "additions": sum(f.additions for f in files),
            "deletions": sum(f.deletions for f in files),
            "commits": len([c for c in commit_entries if c["kind"] != "merge"]),
            "agent_commits": len([c for c in commit_entries if c["kind"] == "agent"]),
            "developer_commits": len([c for c in commit_entries if c["kind"] == "developer"]),
            "pending_commits": len(pending),
            "sessions": len(sessions),
            "runs": sum(len(s["runs"]) for s in sessions),
            "systems": len(feature["systems"]),
            "tests": len((feature.get("tests") or {}).get("tests", [])),
            "decisions": len(feature["decisions"]),
            "open_legs": len([leg for leg in legs if not leg.get("closed_at")]),
        }
        return {
            "evidence_version": EVIDENCE_VERSION,
            "computed_at": util.now_iso(),
            "debrief_version": __version__,
            "project_id": self.project_id,
            "feature_id": self.feature_id,
            "repo_available": True,
            "branch": branch,
            "base": base,
            "effective_base": eff_base,
            "head": self.head_rev,
            "head_commit": head_commit,
            "head_tree": gitutil.tree_of(self.repo, head_commit) if head_commit else None,
            "worktree": str(self.worktree) if self.worktree else None,
            "patch": patch_name,
            "patch_id": feature_patch_id,
            "legs": leg_entries,
            "commits": commit_entries,
            "pending_commits": pending,
            "files": file_entries,
            "anchors": anchors,
            "coverage": coverage,
            "tests": test_evidence,
            "critical_paths": cp_status,
            "flags": flags,
            "queue": queue,
            "issues": records.all_issues(feature),
            "stats": stats,
        }

    @staticmethod
    def _logged(legs: List[dict]) -> Tuple[Dict[str, dict], Dict[str, dict]]:
        by_sha: Dict[str, dict] = {}
        by_pid: Dict[str, dict] = {}
        for leg in legs:
            for entry in leg.get("commits", []):
                info = dict(entry, leg_id=leg["leg_id"])
                if entry.get("sha"):
                    by_sha[entry["sha"]] = info
                if entry.get("patch_id"):
                    by_pid[entry["patch_id"]] = info
        return by_sha, by_pid

    def _store_file_versions(self, files: List[diffparse.FilePatch]) -> None:
        for fp in files:
            if fp.binary:
                continue
            self.store_blob(fp.old_blob)
            if self.head_rev == "WORKTREE" and fp.new_path:
                try:
                    data = (self.worktree / fp.new_path).read_bytes()
                except OSError:
                    data = None
                if data is not None:
                    fp.new_blob = git_blob_id(data)
                    self.store_blob(fp.new_blob, data)
            else:
                self.store_blob(fp.new_blob)

    # --- anchors -------------------------------------------------------------------------------

    def _anchor_sources(self, feature: dict):
        for system in feature["systems"]:
            sid = system["meta"]["id"]
            for i, anchor in enumerate(system["meta"]["anchors"]):
                yield {"record": system["path"], "owner": sid, "owner_kind": "system", "index": i, "anchor": anchor,
                       "critical_path": None}
            for cp in system["meta"]["critical_paths"]:
                if cp.get("anchor"):
                    yield {"record": system["path"], "owner": sid, "owner_kind": "system", "index": None,
                           "anchor": cp["anchor"], "critical_path": cp["id"]}
        tests = feature.get("tests") or {}
        for test in tests.get("tests", []):
            for i, anchor in enumerate(test["anchors"]):
                yield {"record": tests.get("path", "tests.yaml"), "owner": test["id"], "owner_kind": "test", "index": i,
                       "anchor": anchor, "critical_path": None}

    def _relativize(self, path: str, feature: dict) -> str:
        if not path.startswith("/"):
            return path
        for sess in feature["sessions"]:
            root = (sess["meta"].get("repo_root") or "").rstrip("/")
            if root and path.startswith(root + "/"):
                return path[len(root) + 1:]
        return path

    def _resolve_anchors(self, feature: dict, base: str) -> List[dict]:
        resolved = []
        for src in self._anchor_sources(feature):
            anchor = dict(src["anchor"])
            anchor["path"] = self._relativize(anchor["path"], feature)
            removed = anchor.get("role") == "removed"
            rev = base if removed else self.head_rev
            text = self.text_at(rev, anchor["path"]) if rev else None
            exists = text is not None or (not removed and self._path_is_dir(anchor["path"]))
            status, rng, reason = "stale", None, None
            if text is None and not exists:
                reason = "file not found at the feature's " + ("base" if removed else "head")
            else:
                line_count = len(text.splitlines()) if text is not None else 0
                if anchor.get("symbol") and text is not None:
                    syms = symbols.index(anchor["path"], text) if removed else self.symbols_at_head(anchor["path"])
                    found = symbols.find(syms, anchor["symbol"])
                    if found:
                        status, rng = "symbol", [found.start, found.end]
                    else:
                        reason = f"symbol {anchor['symbol']} not found"
                if status == "stale" and anchor.get("lines"):
                    start, end = anchor["lines"]
                    if start <= line_count:
                        status, rng = "lines", [start, min(end, line_count)]
                        if end > line_count:
                            reason = f"range ends past line {line_count}; clamped"
                        elif anchor.get("symbol"):
                            reason = f"symbol {anchor['symbol']} not found; used the line range"
                    else:
                        reason = f"lines {start}-{end} are past the end of the file ({line_count} lines)"
                if status == "stale" and not anchor.get("symbol") and not anchor.get("lines"):
                    status = "path"
            resolved.append({
                "record": src["record"], "owner": src["owner"], "owner_kind": src["owner_kind"],
                "critical_path": src["critical_path"], "index": src["index"],
                "path": anchor["path"], "symbol": anchor.get("symbol"), "lines": anchor.get("lines"),
                "role": anchor.get("role"), "side": "old" if removed else "new",
                "status": status, "range": rng, "reason": reason,
            })
        return resolved

    def _path_is_dir(self, path: str) -> bool:
        if self.head_rev == "WORKTREE" and self.worktree is not None:
            return (self.worktree / path).is_dir()
        out = gitutil.try_run(["cat-file", "-t", f"{self.head_rev}:{path.rstrip('/')}"], self.repo)
        return out == "tree"

    # --- per-file evidence ---------------------------------------------------------------------------

    def _tests_by_target(self, feature: dict) -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = {}
        for test in (feature.get("tests") or {}).get("tests", []):
            for target in test["validates"]:
                out.setdefault(target, []).append(test["id"])
        return out

    def _file_evidence(self, fp: diffparse.FilePatch, anchors: List[dict], incidental: List[str],
                       rule_set: List[rules.Rule], tests_by_target: Dict[str, List[str]]):
        path = fp.path
        noise = diffparse.classify_noise(path)
        is_incidental = records.is_incidental(path, incidental) or (fp.old_path and records.is_incidental(fp.old_path, incidental))
        new_anchors = [a for a in anchors if a["side"] == "new" and a["path"] == path]
        old_anchors = [a for a in anchors if a["side"] == "old" and fp.old_path and a["path"] == fp.old_path]
        dir_anchors = [a for a in anchors if a["status"] == "path" and path.startswith(a["path"].rstrip("/") + "/")]
        file_flags = rules.scan(rule_set, path, fp.hunks) if not (fp.binary or noise) else []
        cp_ranges = [a for a in new_anchors if a["critical_path"] and a["range"]]
        hunks_out, units = [], []
        hunk_list = fp.hunks or [None]
        for hunk in hunk_list:
            claims: List[dict] = []
            cps: List[str] = []
            if hunk is not None:
                rng_new = hunk.new_range()
                rng_old = hunk.old_range()
                for a in new_anchors:
                    if a["range"] and (fp.status != "D") and diffparse.ranges_overlap(a["range"], tuple(rng_new)):
                        claims.append({"by": a["owner"], "kind": a["owner_kind"], "strength": "strong",
                                       "critical_path": a["critical_path"]})
                        if a["critical_path"]:
                            cps.append(f"{a['owner']}/{a['critical_path']}")
                for a in old_anchors:
                    if a["range"] and diffparse.ranges_overlap(a["range"], tuple(rng_old)):
                        claims.append({"by": a["owner"], "kind": a["owner_kind"], "strength": "strong", "critical_path": None})
                hid = hunk.id
            else:
                hid = util.short_hash(f"{path}\n{fp.old_blob}\n{fp.new_blob}\n{fp.status}")
                for a in new_anchors + old_anchors:
                    if a["status"] != "stale":
                        claims.append({"by": a["owner"], "kind": a["owner_kind"],
                                       "strength": "strong" if fp.status in ("R", "C") or a["range"] else "weak",
                                       "critical_path": None})
            if not any(c["strength"] == "strong" for c in claims):
                for a in new_anchors + old_anchors + dir_anchors:
                    if a["status"] == "path":
                        claims.append({"by": a["owner"], "kind": a["owner_kind"], "strength": "weak", "critical_path": None})
            claims = _dedupe_claims(claims)
            strong = any(c["strength"] == "strong" for c in claims)
            if strong:
                state = "covered"
            elif is_incidental or noise:
                state = "incidental"
            elif claims:
                state = "weak"
            else:
                state = "unclaimed"
            systems_claiming = sorted({c["by"] for c in claims if c["kind"] == "system" and c["strength"] == "strong"})
            tests = sorted({t for sid in systems_claiming for t in tests_by_target.get(sid, [])}
                           | {t for cp in cps for t in tests_by_target.get(cp, [])}
                           | {c["by"] for c in claims if c["kind"] == "test"})
            hunk_flags = [f for f in file_flags if hunk is not None and f["hunk_id"] == hid]
            for flag in hunk_flags:
                if flag["category"] == "critical":
                    flag["declared"] = any(diffparse.ranges_overlap(a["range"], (flag["line"], flag["line"]))
                                           for a in cp_ranges) if flag["side"] == "new" else False
            hunk_entry = {
                "id": hid,
                "state": state,
                "claims": claims,
                "critical_paths": sorted(set(cps)),
                "tests": tests,
                "flags": [f["rule"] for f in hunk_flags if not f.get("declared")],
            }
            if hunk is not None:
                hunk_entry.update({
                    "old_start": hunk.old_start, "old_len": hunk.old_len,
                    "new_start": hunk.new_start, "new_len": hunk.new_len,
                    "additions": hunk.additions, "deletions": hunk.deletions,
                    "whitespace_only": hunk.whitespace_only(),
                })
                enclosing = symbols.enclosing(self.symbols_at_head(path), hunk.new_range()[0]) if fp.status != "D" else None
                hunk_entry["symbol"] = enclosing.name if enclosing else None
            hunks_out.append(hunk_entry)
            units.append({"path": path, "hunk": hid, "state": state,
                          "size": (hunk.additions + hunk.deletions) if hunk is not None else 1})
        entry = fp.to_dict(with_lines=False)
        entry["hunks"] = hunks_out
        entry["noise"] = noise
        entry["incidental"] = bool(is_incidental)
        entry["systems"] = sorted({c["by"] for h in hunks_out for c in h["claims"] if c["kind"] == "system"})
        return entry, file_flags, units

    @staticmethod
    def _coverage(units: List[dict]) -> dict:
        counts = {"covered": 0, "incidental": 0, "weak": 0, "unclaimed": 0}
        for unit in units:
            counts[unit["state"]] += 1
        total = len(units)
        explained = counts["covered"] + counts["incidental"]
        return dict(counts, units=total, ratio=(round(explained / total, 4) if total else None))

    # --- tests -----------------------------------------------------------------------------------------

    @staticmethod
    def _norm_cmd(cmd: str) -> str:
        return " ".join((cmd or "").replace("\\\n", " ").split())

    def _test_evidence(self, feature: dict, head: Optional[str]) -> List[dict]:
        runs = []
        for sess in feature["sessions"]:
            for run in sess["runs"]:
                runs.append(run)
        runs.sort(key=lambda r: r.get("started_at") or "")
        out = []
        for test in (feature.get("tests") or {}).get("tests", []):
            wanted = self._norm_cmd(test["command"])
            matches = []
            if wanted:
                for run in runs:
                    got = self._norm_cmd(run.get("command", ""))
                    if got == wanted or got.startswith(wanted + " ") or (len(wanted) > 6 and wanted in got):
                        matches.append(run)
            latest = matches[-1] if matches else None
            if latest is not None:
                status = "verified_pass" if latest.get("exit_code") == 0 else "verified_fail"
            elif test["kind"] == "manual":
                status = "manual"
            elif test["claimed_result"] == "pass":
                status = "claimed_only"
            elif test["claimed_result"] == "fail":
                status = "claimed_fail"
            else:
                status = "not_run"
            out.append({
                "id": test["id"],
                "status": status,
                "claimed_result": test["claimed_result"],
                "command": test["command"],
                "current": bool(latest and head and latest.get("head") == head and not latest.get("dirty")),
                "runs": [{"file": r.get("file"), "command": r.get("command"), "exit_code": r.get("exit_code"),
                          "started_at": r.get("started_at"), "duration_s": r.get("duration_s"), "head": r.get("head"),
                          "session_id": r.get("session_id")} for r in matches[-10:]],
            })
        return out

    def _critical_path_status(self, feature: dict, test_evidence: List[dict]) -> List[dict]:
        tests = (feature.get("tests") or {})
        by_id = {t["id"]: t for t in test_evidence}
        gaps = {g["validates"] for g in tests.get("gaps", [])}
        out = []
        for system in feature["systems"]:
            sid = system["meta"]["id"]
            for cp in system["meta"]["critical_paths"]:
                ref = f"{sid}/{cp['id']}"
                covering = [t["id"] for t in tests.get("tests", []) if ref in t["validates"]]
                statuses = [by_id[t]["status"] for t in covering if t in by_id]
                out.append({
                    "ref": ref, "system": sid, "id": cp["id"], "kind": cp["kind"], "invariant": cp["invariant"],
                    "tests": covering,
                    "status": "tested" if covering else ("gap" if ref in gaps or sid in gaps else "untested"),
                    "verified": any(s == "verified_pass" for s in statuses),
                })
        return out

    # --- commits ----------------------------------------------------------------------------------------

    def _commits_and_legs(self, feature: dict, head: Optional[str], pending: List[str], logged_shas, logged_pids,
                          anchors: List[dict]):
        legs = feature["legs"]
        depends = {}
        for system in feature["systems"]:
            depends[system["meta"]["id"]] = {d["system"] for d in system["meta"]["depends_on"]}
        leg_entries, commit_entries = [], []
        seen: Set[str] = set()
        for i, leg in enumerate(legs):
            leg_base = leg.get("base_ref")
            closed = bool(leg.get("closed_at"))
            if closed:
                leg_head = leg.get("head_commit") or (leg.get("head_ref") if leg.get("head_ref") != "WORKTREE" else None)
            else:
                leg_head = head
            shas: List[str] = []
            if leg_base and leg_head and self.object_exists(leg_head):
                eff = self._effective_base(leg_base, leg_head) if i == 0 else leg_base
                shas = [s for s in gitutil.rev_list(self.repo, f"{eff}..{leg_head}") if s not in seen]
                if closed or self.head_rev != "WORKTREE":
                    patch_name = f"{eff}..{leg_head}.patch"
                    target = self.evidence_dir / patch_name
                    if not target.exists() or not closed:
                        util.write_text(target, self._diff(eff, leg_head))
                else:
                    patch_name = None
            else:
                patch_name = None
            if not closed and self.head_rev == "WORKTREE" and leg_base:
                patch_name = f"{leg_base}..WORKTREE.patch"
                util.write_text(self.evidence_dir / patch_name, self._diff(leg_base, "WORKTREE"))
            seen.update(shas)
            for sha in shas:
                commit_entries.append(self._commit(sha, leg["leg_id"], logged_shas, logged_pids, anchors, depends))
            leg_entries.append({
                "leg_id": leg["leg_id"],
                "base": leg_base,
                "head": "WORKTREE" if (not closed and self.head_rev == "WORKTREE") else leg_head,
                "head_commit": leg_head,
                "open": not closed,
                "opened_at": leg.get("opened_at"),
                "closed_at": leg.get("closed_at"),
                "closed_by": leg.get("closed_by"),
                "close_requested_at": leg.get("close_requested_at"),
                "close_requested_by": leg.get("close_requested_by"),
                "sessions": [s.get("session_id") if isinstance(s, dict) else s for s in leg.get("sessions", [])],
                "commits": shas,
                "patch": patch_name,
            })
        if pending:
            for sha in pending:
                if sha not in seen:
                    commit_entries.append(self._commit(sha, None, logged_shas, logged_pids, anchors, depends))
        return leg_entries, commit_entries

    def _commit(self, sha: str, leg_id: Optional[str], logged_shas, logged_pids, anchors, depends) -> dict:
        path = self.evidence_dir / "commits" / f"{sha}.json"
        stored = util.read_json(path, None)
        if isinstance(stored, dict) and stored.get("evidence_version") == EVIDENCE_VERSION and stored.get("patch") is not None:
            meta = {k: stored.get(k) for k in ("sha", "parents", "author", "author_email", "authored_at", "committer",
                                                 "committed_at", "tree", "subject", "body")}
            patch_text = stored["patch"]
            pid = stored.get("patch_id")
        else:
            meta = gitutil.commit_meta(self.repo, sha) or {"sha": sha, "parents": [], "subject": "", "body": ""}
            merge = len(meta.get("parents") or []) > 1
            patch_text = "" if merge else gitutil.commit_patch(self.repo, sha)
            pid = None if merge else gitutil.patch_id(self.repo, patch_text)
        merge = len(meta.get("parents") or []) > 1
        logged = logged_shas.get(sha) or (logged_pids.get(pid) if pid else None)
        kind = "merge" if merge else ("agent" if logged else "developer")
        files = diffparse.parse(patch_text) if patch_text else []
        systems_hit: Set[str] = set()
        file_rows = []
        for fp in files:
            if not fp.binary:
                self.store_blob(fp.old_blob)
                self.store_blob(fp.new_blob)
            hit = self._commit_file_systems(fp, anchors)
            systems_hit.update(hit)
            file_rows.append({"path": fp.path, "old_path": fp.old_path, "status": fp.status, "binary": fp.binary,
                              "additions": fp.additions, "deletions": fp.deletions, "systems": sorted(hit),
                              "old_blob": fp.old_blob, "new_blob": fp.new_blob})
        subject = meta.get("subject") or ""
        body = meta.get("body") or ""
        thin = kind == "agent" and (not body.strip() or len(subject) > 72)
        non_atomic = False
        if kind == "agent" and len(systems_hit) > 1:
            hit = sorted(systems_hit)
            for a in hit:
                for b in hit:
                    if a < b and b not in depends.get(a, set()) and a not in depends.get(b, set()):
                        non_atomic = True
        record = dict(meta)
        record.update({
            "evidence_version": EVIDENCE_VERSION,
            "project_id": self.project_id,
            "feature_id": self.feature_id,
            "leg_id": leg_id,
            "kind": kind,
            "session_id": (logged or {}).get("session_id"),
            "rebased_from": (logged or {}).get("sha") if logged and logged.get("sha") != sha else None,
            "patch_id": pid,
            "patch": patch_text,
            "files": file_rows,
            "systems": sorted(systems_hit),
            "thin_message": thin,
            "non_atomic": non_atomic,
        })
        util.write_json(path, record)
        return {k: record[k] for k in ("sha", "leg_id", "kind", "session_id", "subject", "committed_at", "author",
                                       "systems", "thin_message", "non_atomic", "rebased_from")} | {
            "body": body, "files": [{"path": r["path"], "status": r["status"], "additions": r["additions"],
                                     "deletions": r["deletions"]} for r in file_rows]}

    def _commit_file_systems(self, fp: diffparse.FilePatch, anchors: List[dict]) -> Set[str]:
        """Systems whose head anchors overlap this commit's hunks, mapped to head lines."""
        path = fp.path
        file_anchors = [a for a in anchors if a["owner_kind"] == "system" and a["side"] == "new"
                        and a["path"] == path and a["range"]]
        if not file_anchors or fp.binary or fp.status == "D":
            return set()
        commit_text = self.text_of_blob(fp.new_blob)
        head_text = self.text_at(self.head_rev, path)
        if commit_text is None or head_text is None:
            return set()
        mapping = diffparse.line_map(commit_text, head_text) if commit_text != head_text else None
        hit: Set[str] = set()
        for hunk in fp.hunks:
            start, end = hunk.new_range()
            rng = (start, end) if mapping is None else diffparse.map_range(mapping, start, end)
            if rng is None:
                continue
            for a in file_anchors:
                if diffparse.ranges_overlap(a["range"], rng):
                    hit.add(a["owner"])
        return hit

    # --- review queue ------------------------------------------------------------------------------

    def _queue(self, feature, files, flags, anchors, tests, cp_status, commits) -> List[dict]:
        items: List[dict] = []

        def add(kind, severity, title, detail="", undeclared=True, **extra):
            ident = util.short_hash(json.dumps([kind, title, extra.get("path"), extra.get("line"), extra.get("hunk_id"),
                                                extra.get("commit"), extra.get("record")], sort_keys=True))
            items.append(dict({"id": f"{kind}:{ident}", "kind": kind, "severity": severity, "undeclared": undeclared,
                               "title": title, "detail": detail}, **extra))

        for flag in flags:
            if flag["category"] == "critical":
                if flag.get("declared"):
                    continue
                add("undeclared-critical", flag["severity"], f"Undeclared {flag['kind']}: {flag['text'][:80]}",
                    flag["message"], path=flag["path"], line=flag["line"], side=flag["side"], hunk_id=flag["hunk_id"],
                    rule=flag["rule"], near=self._near(flag["path"], flag["line"], anchors))
            elif flag["category"] == "test":
                add("weakened-test", flag["severity"], flag["message"], flag["text"], path=flag["path"],
                    line=flag["line"], side=flag["side"], hunk_id=flag["hunk_id"], rule=flag["rule"])
            else:
                add("risky", flag["severity"], flag["message"], flag["text"], path=flag["path"], line=flag["line"],
                    side=flag["side"], hunk_id=flag["hunk_id"], rule=flag["rule"],
                    near=self._near(flag["path"], flag["line"], anchors))
        for f in files:
            for h in f["hunks"]:
                line = h.get("new_start") if f["status"] != "D" else h.get("old_start")
                if h["state"] == "unclaimed" and not h.get("whitespace_only"):
                    size = h.get("additions", 0) + h.get("deletions", 0)
                    add("unclaimed-hunk", "high" if size >= 30 else "medium",
                        f"Unexplained change in {f['path']}" + (f" ({h['symbol']})" if h.get("symbol") else ""),
                        f"{size} changed lines that no system anchors and the brief doesn't list as incidental.",
                        path=f["path"], line=line, hunk_id=h["id"], near=self._near(f["path"], line or 1, anchors))
                elif h["state"] == "weak":
                    add("weak-claim", "low", f"Only a path anchor explains {f['path']}",
                        "Claimed by " + ", ".join(sorted({c['by'] for c in h['claims']})) + " without a symbol or line range.",
                        undeclared=False, path=f["path"], line=line, hunk_id=h["id"])
        for a in anchors:
            if a["status"] == "stale":
                add("stale-anchor", "medium", f"Stale anchor in {a['record']}",
                    f"{a['path']}" + (f" {a['symbol']}" if a.get("symbol") else "") + f": {a['reason']}",
                    undeclared=False, record=a["record"], path=a["path"], owner=a["owner"])
        for t in tests:
            if t["status"] == "verified_fail" and t["claimed_result"] == "pass":
                add("failing-test", "high", f"Test {t['id']} is claimed passing but its last run failed",
                    t["command"], undeclared=True, test=t["id"])
            elif t["status"] == "claimed_only":
                add("claim-only-test", "medium", f"Test {t['id']} is claimed passing with no recorded run",
                    f"No `bin/session run` matched: {t['command']}", undeclared=False, test=t["id"])
        for cp in cp_status:
            if cp["status"] == "untested":
                add("untested-critical-path", "low", f"No test or listed gap for {cp['ref']}", cp["invariant"],
                    undeclared=False, near={"system": cp["system"], "critical_path": cp["id"]})
        for c in commits:
            if c["non_atomic"]:
                add("non-atomic-commit", "low", f"Commit {c['sha'][:9]} spans unrelated systems",
                    f"{c['subject']} touches {', '.join(c['systems'])}, which have no depends_on link.",
                    undeclared=False, commit=c["sha"])
            if c["thin_message"]:
                add("thin-commit-message", "low", f"Thin message on {c['sha'][:9]}",
                    f"\"{c['subject']}\" has " + ("no body" if not (c.get('body') or '').strip() else "a subject over 72 characters"),
                    undeclared=False, commit=c["sha"])
        for issue in records.all_issues(feature):
            add("record-issue", "medium" if issue["level"] == "error" else "low", f"{issue['record']}: {issue['message']}",
                "", undeclared=False, record=issue["record"])
        conflicts = util.read_json(self.project_dir / "conflicts.json", {}) or {}
        prefix = f"features/{self.feature_id}/"
        for conflict in conflicts.get("conflicts", []):
            if conflict.get("path", "").startswith(prefix):
                add("sync-conflict", "medium", f"Sync kept two versions of {conflict['path'][len(prefix):]}",
                    f"Merged on {conflict.get('host')} at {conflict.get('at')}; compare the .conflict copy.",
                    undeclared=False, record=conflict["path"][len(prefix):])
        items.sort(key=lambda i: (SEVERITY_ORDER.get(i["severity"], 9), not i["undeclared"], i.get("path") or "",
                                  i.get("line") or 0))
        return items

    def _near(self, path: str, line: int, anchors: List[dict]) -> Optional[dict]:
        """The nearest explanation for a line: an anchor in the same file."""
        best = None
        for a in anchors:
            if a["path"] != path or not a["range"] or a["owner_kind"] != "system":
                continue
            start, end = a["range"]
            distance = 0 if start <= line <= end else min(abs(line - start), abs(line - end))
            if best is None or distance < best[0]:
                best = (distance, a)
        if best is None:
            return None
        a = best[1]
        return {"system": a["owner"], "critical_path": a["critical_path"], "distance": best[0]}


def _dedupe_claims(claims: List[dict]) -> List[dict]:
    seen = set()
    out = []
    for claim in claims:
        key = (claim["by"], claim["kind"], claim["strength"], claim.get("critical_path"))
        if key not in seen:
            seen.add(key)
            out.append(claim)
    return out


# --- entry points ------------------------------------------------------------------------------------


def ingest_feature(project_id: str, feature_id: str, root: Optional[Path] = None, repo: Optional[Path] = None,
                   update_index: bool = True) -> dict:
    evidence = FeatureIngest(project_id, feature_id, root, repo).run()
    if update_index:
        try:
            from . import index

            index.update_feature(project_id, feature_id, root=root, evidence=evidence)
        except Exception:  # the index is a cache; ingest results stand without it
            pass
    return evidence


def feature_ids(project_id: str, root: Optional[Path] = None) -> List[str]:
    base = paths.project_dir(project_id, root) / "features"
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_dir() and not p.name.startswith("."))


def summary_line(evidence: dict) -> str:
    cov = evidence.get("coverage") or {}
    queue = evidence.get("queue") or []
    high = sum(1 for i in queue if i["severity"] == "high")
    ratio = cov.get("ratio")
    pct = f"{ratio * 100:.0f}%" if isinstance(ratio, (int, float)) else "n/a"
    return (f"coverage {pct} of {cov.get('units', 0)} changed hunks"
            f" ({cov.get('unclaimed', 0)} unexplained, {cov.get('weak', 0)} weak);"
            f" review queue {len(queue)} ({high} high)")


def cli(args) -> int:
    from . import index

    root = paths.archive_root()
    targets: List[Tuple[str, Optional[str]]] = []
    if args.project:
        targets.append((args.project, args.feature))
    elif args.repos:
        for repo in args.repos:
            found = projects.find_project(Path(repo))
            if not found:
                print(f"{repo}: not tracked (run `debrief init {repo}`)")
                continue
            targets.append((found[0], args.feature))
    else:
        for pid in projects.list_projects(root):
            targets.append((pid, args.feature))
    status = 0
    for pid, fid in targets:
        fids = [fid] if fid else feature_ids(pid, root)
        for feature_id in fids:
            try:
                evidence = ingest_feature(pid, feature_id, root)
            except Exception as exc:  # report and continue with the next feature
                print(f"{pid}/{feature_id}: ingest failed: {exc}")
                status = 1
                continue
            if not args.quiet:
                print(f"{pid}/{feature_id}: {summary_line(evidence)}")
    try:
        index.rebuild(root)
    except Exception as exc:
        print(f"index rebuild failed: {exc}")
    return status

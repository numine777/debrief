"""The ``/api/v1`` views: JSON built from the archive.

The local server, the hub and the single-file export all use this module, so
the front end sees the same data shapes everywhere (decision D8). Nothing here
touches a project repository: everything comes from records and evidence.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import __version__, diffparse, index, paths, projects, records, util


class NotFound(Exception):
    pass


class BadRequest(Exception):
    pass


_SAFE_ID = __import__("re").compile(r"^[A-Za-z0-9._-]+$")


def check_id(value: str, what: str) -> str:
    if not value or not _SAFE_ID.match(value) or value in (".", ".."):
        raise BadRequest(f"invalid {what}")
    return value


class Api:
    def __init__(self, root: Optional[Path] = None, mode: str = "local", readonly: bool = False):
        self.root = root or paths.archive_root()
        self.mode = mode
        self.readonly = readonly
        self._patch_cache: Dict[Tuple[str, float], List[diffparse.FilePatch]] = {}
        self._lock = threading.Lock()

    # --- helpers -------------------------------------------------------------------

    def feature_dir(self, pid: str, fid: str) -> Path:
        check_id(pid, "project id")
        check_id(fid, "feature id")
        path = paths.feature_dir(pid, fid, self.root)
        if not path.is_dir():
            raise NotFound(f"no feature {fid} in project {pid}")
        return path

    def evidence(self, pid: str, fid: str) -> dict:
        return util.read_json(self.feature_dir(pid, fid) / "evidence" / "evidence.json", {}) or {}

    def _parse_cached(self, path: Path) -> List[diffparse.FilePatch]:
        try:
            stamp = path.stat().st_mtime
        except OSError:
            return []
        key = (str(path), stamp)
        with self._lock:
            cached = self._patch_cache.get(key)
        if cached is not None:
            return cached
        parsed = diffparse.parse(path.read_text(encoding="utf-8", errors="replace"))
        with self._lock:
            if len(self._patch_cache) > 32:
                self._patch_cache.clear()
            self._patch_cache[key] = parsed
        return parsed

    @staticmethod
    def _sections(rec: Optional[dict]) -> List[dict]:
        if not rec:
            return []
        out = []
        lead = rec["sections"].get("")
        if lead:
            out.append({"title": "", "markdown": lead})
        for title in rec.get("section_order", []):
            out.append({"title": title, "markdown": rec["sections"].get(title, "")})
        return out

    def _ensure_index(self) -> None:
        conn = index.connect(self.root)
        try:
            count = conn.execute("SELECT COUNT(*) FROM features").fetchone()[0]
        finally:
            conn.close()
        if count == 0 and projects.list_projects(self.root):
            index.rebuild(self.root)

    # --- views ----------------------------------------------------------------------

    def meta(self, user: Optional[dict] = None) -> dict:
        return {
            "version": __version__,
            "protocol": records.PROTOCOL,
            "mode": self.mode,
            "readonly": self.readonly,
            "user": user,
            "host": util.hostname(),
            "archive": str(self.root) if self.mode == "local" else None,
        }

    def index_view(self) -> dict:
        self._ensure_index()
        rows = index.list_features(self.root)
        registry = projects.registered_projects(self.root)
        by_project: Dict[str, dict] = {}
        for pid in projects.list_projects(self.root):
            meta = projects.project_meta(pid, self.root)
            by_project[pid] = {
                "project_id": pid,
                "display_name": meta.get("display_name") or pid,
                "remote_url": meta.get("remote_url"),
                "on_this_host": pid in registry,
                "features": [],
            }
        for row in rows:
            project = by_project.get(row["project_id"])
            if project is not None:
                project["features"].append(row)
        epics: Dict[str, int] = {}
        for row in rows:
            if row.get("epic"):
                epics[row["epic"]] = epics.get(row["epic"], 0) + 1
        ordered = sorted(by_project.values(), key=lambda p: max([f["updated_at"] or "" for f in p["features"]] or [""]),
                         reverse=True)
        return {"projects": ordered, "epics": epics}

    def feature(self, pid: str, fid: str) -> dict:
        fdir = self.feature_dir(pid, fid)
        feat = records.load_feature(fdir)
        ev = util.read_json(fdir / "evidence" / "evidence.json", {}) or {}
        brief = feat.get("brief")
        brief_meta = (brief or {}).get("meta", {})
        ev_files = ev.get("files") or []
        tests = feat.get("tests") or {"tests": [], "gaps": [], "issues": []}
        test_status = {t["id"]: t for t in ev.get("tests") or []}
        cp_status = {c["ref"]: c for c in ev.get("critical_paths") or []}
        systems = []
        for system in feat["systems"]:
            meta = system["meta"]
            sid = meta["id"]
            hunks = [h for f in ev_files for h in f["hunks"]
                     if any(c["by"] == sid and c["kind"] == "system" for c in h["claims"])]
            covering = sorted({t["id"] for t in tests["tests"]
                               if sid in t["validates"] or any(v.startswith(sid + "/") for v in t["validates"])})
            systems.append({
                "id": sid,
                "title": meta.get("title") or sid,
                "change": meta.get("change"),
                "depends_on": meta.get("depends_on", []),
                "anchors": meta.get("anchors", []),
                "critical_paths": [dict(cp, status=cp_status.get(f"{sid}/{cp['id']}", {}).get("status"),
                                        verified=cp_status.get(f"{sid}/{cp['id']}", {}).get("verified"),
                                        tests=cp_status.get(f"{sid}/{cp['id']}", {}).get("tests", []))
                                   for cp in meta.get("critical_paths", [])],
                "decisions": meta.get("decisions", []),
                "sections": self._sections(system),
                "issues": system["issues"],
                "path": system["path"],
                "hunk_count": len(hunks),
                "lines_changed": sum(h.get("additions", 0) + h.get("deletions", 0) for h in hunks),
                "files": sorted({f["path"] for f in ev_files for h in f["hunks"]
                                 if any(c["by"] == sid and c["kind"] == "system" for c in h["claims"])}),
                "tests": covering,
                "test_statuses": [test_status[t]["status"] for t in covering if t in test_status],
            })
        sessions = []
        for sess in feat["sessions"]:
            meta = {k: v for k, v in sess["meta"].items() if k not in ("last_seen_head",)}
            sessions.append({"meta": meta, "journal": sess["journal"], "runs": sess["runs"], "issues": sess["issues"],
                             "path": sess["path"]})
        ev_legs = {leg["leg_id"]: leg for leg in ev.get("legs") or []}
        legs = []
        for leg in feat["legs"]:
            merged = dict(leg)
            merged["evidence"] = ev_legs.get(leg["leg_id"])
            legs.append(merged)
        decisions = [{
            "id": d["meta"]["id"], "title": d["meta"].get("title"), "status": d["meta"].get("status"),
            "reversibility": d["meta"].get("reversibility"), "systems": d["meta"].get("systems", []),
            "sections": self._sections(d), "issues": d["issues"], "path": d["path"],
        } for d in feat["decisions"]]
        landed = util.read_json(fdir / "evidence" / "landed.json", {}) or {}
        squash_record = fdir / "squash" / "record.md"
        compaction = fdir / "squash" / "compaction.md"
        project_meta = projects.project_meta(pid, self.root)
        summary_files = [{k: f.get(k) for k in ("path", "old_path", "status", "binary", "additions", "deletions",
                                                "noise", "incidental", "systems")}
                         | {"hunks": [{k: h.get(k) for k in ("id", "state", "additions", "deletions", "new_start",
                                                               "old_start", "symbol", "flags", "critical_paths",
                                                               "tests", "whitespace_only")}
                                      | {"claims": h.get("claims", [])} for h in f["hunks"]]}
                         for f in ev_files]
        evidence = {k: ev.get(k) for k in ("computed_at", "repo_available", "branch", "base", "effective_base", "head",
                                           "head_commit", "coverage", "stats", "tests", "critical_paths", "queue",
                                           "anchors", "commits", "legs", "pending_commits", "note", "patch")}
        evidence["files"] = summary_files
        return {
            "project": {"project_id": pid, "display_name": project_meta.get("display_name") or pid,
                        "remote_url": project_meta.get("remote_url")},
            "feature_id": fid,
            "title": brief_meta.get("title") or fid,
            "status": brief_meta.get("status") or "in_progress",
            "epic": brief_meta.get("epic"),
            "branch": legs[-1].get("branch") if legs else None,
            "brief": {
                "meta": brief_meta,
                "sections": self._sections(brief),
                "issues": (brief or {}).get("issues", []),
                "exists": brief is not None,
            },
            "systems": systems,
            "tests": {"tests": tests["tests"], "gaps": tests["gaps"], "issues": tests.get("issues", []),
                      "exists": feat.get("tests") is not None},
            "decisions": decisions,
            "legs": legs,
            "sessions": sessions,
            "evidence": evidence,
            "landed": landed.get("commits", []),
            "squash": {
                "record": squash_record.read_text(encoding="utf-8") if squash_record.exists() else None,
                "compaction": compaction.read_text(encoding="utf-8") if compaction.exists() else None,
            },
            "issues": records.all_issues(feat),
        }

    def diff(self, pid: str, fid: str, scope: str = "feature") -> dict:
        fdir = self.feature_dir(pid, fid)
        ev = util.read_json(fdir / "evidence" / "evidence.json", {}) or {}
        if scope == "feature":
            patch_name = ev.get("patch")
            if not patch_name:
                return {"scope": scope, "files": [], "base": None, "head": None}
            parsed = self._parse_cached(fdir / "evidence" / patch_name)
            notes = {f["path"]: f for f in ev.get("files") or []}
            files = []
            for fp in parsed:
                note = notes.get(fp.path, {})
                by_id = {h["id"]: h for h in note.get("hunks", [])}
                entry = fp.to_dict(with_lines=True)
                for key in ("noise", "incidental", "systems"):
                    entry[key] = note.get(key)
                entry["new_blob"] = note.get("new_blob") or entry["new_blob"]
                for hunk in entry["hunks"]:
                    hunk.update({k: v for k, v in by_id.get(hunk["id"], {}).items() if k not in hunk})
                if not entry["hunks"] and note.get("hunks"):
                    entry["file_unit"] = note["hunks"][0]
                files.append(entry)
            return {"scope": scope, "base": ev.get("effective_base"), "head": ev.get("head"), "files": files}
        if scope.startswith("commit:"):
            sha = scope.split(":", 1)[1]
            check_id(sha, "commit")
            record = util.read_json(fdir / "evidence" / "commits" / f"{sha}.json", None)
            if not isinstance(record, dict):
                raise NotFound(f"commit {sha} is not in this feature")
            return self._commit_diff(record)
        raise BadRequest(f"unknown diff scope {scope}")

    def _commit_diff(self, record: dict) -> dict:
        parsed = diffparse.parse(record.get("patch") or "")
        rows = {f["path"]: f for f in record.get("files", [])}
        files = []
        for fp in parsed:
            row = rows.get(fp.path, {})
            by_id = {h["id"]: h for h in row.get("hunks", [])}
            entry = fp.to_dict(with_lines=True)
            entry["systems"] = row.get("systems", [])
            entry["noise"] = diffparse.classify_noise(fp.path)
            for hunk in entry["hunks"]:
                hunk.update({k: v for k, v in by_id.get(hunk["id"], {}).items() if k not in hunk})
            files.append(entry)
        parents = record.get("parents") or []
        return {"scope": f"commit:{record['sha']}", "base": parents[0] if parents else None, "head": record["sha"],
                "files": files}

    def blob(self, pid: str, fid: str, blob_id: str) -> bytes:
        check_id(blob_id, "blob id")
        path = self.feature_dir(pid, fid) / "evidence" / "blobs" / blob_id
        if not path.is_file():
            raise NotFound("that file version is not archived")
        return path.read_bytes()

    def commit(self, sha: str) -> dict:
        sha = sha.strip().lower()
        if len(sha) < 4 or not all(c in "0123456789abcdef" for c in sha):
            raise BadRequest("give at least 4 hex characters of a commit SHA")
        self._ensure_index()
        rows = index.lookup_commit(sha, self.root)
        if not rows:
            raise NotFound(f"no feature contains a commit starting {sha}")
        features, landed = [], []
        for row in rows:
            fdir = paths.feature_dir(row["project_id"], row["feature_id"], self.root)
            title = (records.load_brief(fdir) or {}).get("meta", {}).get("title") or row["feature_id"]
            if row["match"] == "landed":
                ev_legs = (util.read_json(fdir / "evidence" / "evidence.json", {}) or {}).get("legs", [])
                landed.append(dict(row, title=title, legs=[{"leg_id": leg["leg_id"], "commits": leg.get("commits", []),
                                                            "closed_at": leg.get("closed_at")} for leg in ev_legs]))
                continue
            record = util.read_json(fdir / "evidence" / "commits" / f"{row['sha']}.json", None)
            if not isinstance(record, dict):
                continue
            feat = records.load_feature(fdir)
            leg_sessions = [s for s in feat["sessions"] if s["meta"].get("leg_id") == record.get("leg_id")]
            journal = []
            runs = []
            for sess in leg_sessions:
                sid = sess["meta"].get("session_id")
                journal += [dict(e, session_id=sid) for e in sess["journal"]]
                runs += [{k: r.get(k) for k in ("command", "exit_code", "started_at", "duration_s", "head", "file")}
                         | {"session_id": sid} for r in sess["runs"]]
            meta = {k: v for k, v in record.items() if k not in ("patch", "files")}
            features.append({
                "project_id": row["project_id"], "feature_id": row["feature_id"], "title": title,
                "commit": meta, "diff": self._commit_diff(record), "journal": journal, "runs": runs,
            })
        if not features and not landed:
            raise NotFound(f"no archived record for {sha}")
        return {"sha": (features[0]["commit"]["sha"] if features else landed[0]["sha"]), "features": features,
                "landed": landed}

    def search(self, query: str) -> dict:
        query = (query or "").strip()
        if not query:
            return {"query": query, "results": []}
        self._ensure_index()
        return {"query": query, "results": index.search(query, root=self.root)}

    def epic(self, name: str) -> dict:
        self._ensure_index()
        rows = [r for r in index.list_features(self.root) if r.get("epic") == name]
        if not rows:
            raise NotFound(f"no features in epic {name}")
        members = []
        for row in sorted(rows, key=lambda r: r.get("updated_at") or ""):
            fdir = paths.feature_dir(row["project_id"], row["feature_id"], self.root)
            feat = records.load_feature(fdir)
            brief = feat.get("brief") or {}
            built = records.find_section(brief.get("sections", {}), "What was built") or ""
            started = [s["meta"].get("started_at") for s in feat["sessions"] if s["meta"].get("started_at")]
            members.append({
                "project_id": row["project_id"], "feature_id": row["feature_id"], "title": row["title"],
                "status": row["status"], "coverage": row["coverage"], "started_at": min(started) if started else None,
                "intent": records.find_section(brief.get("sections", {}), "Intent") or "",
                "outcome": built.split("\n\n")[0],
                "systems": [{"id": s["meta"]["id"], "title": s["meta"].get("title"), "change": s["meta"].get("change"),
                             "depends_on": s["meta"].get("depends_on", [])} for s in feat["systems"]],
            })
        members.sort(key=lambda m: m["started_at"] or "")
        return {"epic": name, "features": members}

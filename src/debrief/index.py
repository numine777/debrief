"""The SQLite index: a derived cache over the archive, rebuilt on demand.

It answers what the archive layout can't answer quickly: which feature and leg
a commit SHA belongs to, which squash commit landed a feature, and full-text
search across systems, files, symbols and journals. It never syncs.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional

from . import paths, records, util

SCHEMA_VERSION = "2"
SCHEMA = """
CREATE TABLE IF NOT EXISTS features (
  project_id TEXT, feature_id TEXT, title TEXT, status TEXT, epic TEXT, branch TEXT,
  updated_at TEXT, coverage REAL, units INTEGER, covered INTEGER, incidental INTEGER, weak INTEGER,
  unclaimed INTEGER, legs INTEGER, open_legs INTEGER, open_leg TEXT, open_leg_commits INTEGER,
  close_requested_at TEXT, queue INTEGER, high INTEGER, sessions INTEGER, head TEXT, base TEXT,
  landed TEXT, summary TEXT, PRIMARY KEY (project_id, feature_id));
CREATE TABLE IF NOT EXISTS commits (
  sha TEXT, project_id TEXT, feature_id TEXT, leg_id TEXT, kind TEXT, subject TEXT, committed_at TEXT,
  PRIMARY KEY (sha, project_id, feature_id));
CREATE INDEX IF NOT EXISTS commits_sha ON commits (sha);
CREATE TABLE IF NOT EXISTS landed (
  sha TEXT, project_id TEXT, feature_id TEXT, method TEXT, confidence REAL, detail TEXT, landed_at TEXT,
  PRIMARY KEY (sha, project_id, feature_id));
CREATE TABLE IF NOT EXISTS search (
  project_id TEXT, feature_id TEXT, kind TEXT, ref TEXT, title TEXT, body TEXT);
CREATE INDEX IF NOT EXISTS search_feature ON search (project_id, feature_id);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def connect(root: Optional[Path] = None) -> sqlite3.Connection:
    path = paths.index_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError:
        pass
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    row = conn.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
    if row is None or row[0] != SCHEMA_VERSION:
        # A cache: drop and rebuild rather than migrate.
        for table in ("features", "commits", "landed", "search"):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.executescript(SCHEMA)
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('schema', ?)", (SCHEMA_VERSION,))
        conn.execute("DELETE FROM meta WHERE key LIKE 'default_tip:%'")
        conn.commit()
    else:
        conn.executescript(SCHEMA)
    return conn


def _evidence(project_id: str, feature_id: str, root: Optional[Path]) -> dict:
    return util.read_json(paths.feature_dir(project_id, feature_id, root) / "evidence" / "evidence.json", {}) or {}


def update_feature(project_id: str, feature_id: str, root: Optional[Path] = None, evidence: Optional[dict] = None,
                   conn: Optional[sqlite3.Connection] = None) -> None:
    own = conn is None
    conn = conn or connect(root)
    try:
        feature_dir = paths.feature_dir(project_id, feature_id, root)
        feature = records.load_feature(feature_dir)
        evidence = evidence if evidence is not None else _evidence(project_id, feature_id, root)
        _write_feature(conn, project_id, feature_id, feature, evidence)
        conn.commit()
    finally:
        if own:
            conn.close()


def _write_feature(conn: sqlite3.Connection, project_id: str, feature_id: str, feature: dict, evidence: dict) -> None:
    brief = (feature.get("brief") or {}).get("meta", {})
    legs = feature["legs"]
    cov = evidence.get("coverage") or {}
    queue = evidence.get("queue") or []
    open_legs = [leg for leg in legs if not leg.get("closed_at")]
    open_commits = sum(len(leg.get("commits", [])) for leg in (evidence.get("legs") or []) if leg.get("open"))
    branch = legs[-1].get("branch") if legs else None
    stamps = [leg.get("closed_at") or leg.get("opened_at") or "" for leg in legs]
    stamps += [s["meta"].get("ended_at") or s["meta"].get("started_at") or "" for s in feature["sessions"]]
    updated = max([s for s in stamps if s] or [evidence.get("computed_at") or ""])
    intent = records.find_section((feature.get("brief") or {}).get("sections", {}), "Intent") or ""
    conn.execute("DELETE FROM features WHERE project_id=? AND feature_id=?", (project_id, feature_id))
    landed = util.read_json(Path(feature["path"]) / "evidence" / "landed.json", None) or {}
    landed_sha = (landed.get("commits") or [{}])[-1].get("sha") if landed.get("commits") else None
    open_leg = open_legs[-1] if open_legs else None
    conn.execute(
        "INSERT INTO features VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (project_id, feature_id, brief.get("title") or feature_id, brief.get("status") or "in_progress",
         brief.get("epic"), branch, updated, cov.get("ratio"), cov.get("units"), cov.get("covered"),
         cov.get("incidental"), cov.get("weak"), cov.get("unclaimed"), len(legs), len(open_legs),
         open_leg["leg_id"] if open_leg else None, open_commits,
         open_leg.get("close_requested_at") if open_leg else None, len(queue),
         sum(1 for i in queue if i["severity"] == "high"), len(feature["sessions"]), evidence.get("head"),
         evidence.get("effective_base") or evidence.get("base"), landed_sha, intent[:400]),
    )
    conn.execute("DELETE FROM commits WHERE project_id=? AND feature_id=?", (project_id, feature_id))
    for commit in evidence.get("commits") or []:
        conn.execute("INSERT OR REPLACE INTO commits VALUES (?,?,?,?,?,?,?)",
                     (commit["sha"], project_id, feature_id, commit.get("leg_id"), commit.get("kind"),
                      commit.get("subject"), commit.get("committed_at")))
    conn.execute("DELETE FROM search WHERE project_id=? AND feature_id=?", (project_id, feature_id))
    rows = []
    if feature.get("brief"):
        rows.append(("brief", "brief", brief.get("title") or feature_id, feature["brief"]["body"]))
    for system in feature["systems"]:
        meta = system["meta"]
        symbols = " ".join(a.get("symbol") or "" for a in meta["anchors"])
        paths_text = " ".join(a["path"] for a in meta["anchors"])
        cps = " ".join(f"{cp['id']} {cp['invariant']}" for cp in meta["critical_paths"])
        rows.append(("system", meta["id"], meta.get("title") or meta["id"],
                     f"{system['body']}\n{symbols}\n{paths_text}\n{cps}"))
    for decision in feature["decisions"]:
        rows.append(("decision", decision["meta"]["id"], decision["meta"].get("title") or "", decision["body"]))
    for test in (feature.get("tests") or {}).get("tests", []):
        rows.append(("test", test["id"], test["claim"], f"{test['command']} {' '.join(test['validates'])}"))
    for sess in feature["sessions"]:
        sid = sess["meta"].get("session_id") or sess["path"]
        for entry in sess["journal"]:
            rows.append(("journal", f"{sid}#{entry['line']}", f"{entry['kind']} · {entry['at']}", entry["text"]))
    for f in evidence.get("files") or []:
        syms = " ".join(sorted({h.get("symbol") or "" for h in f.get("hunks", [])}))
        rows.append(("file", f["path"], f["path"], syms))
    for commit in evidence.get("commits") or []:
        rows.append(("commit", commit["sha"], commit.get("subject") or "", commit.get("body") or ""))
    conn.executemany("INSERT INTO search VALUES (?,?,?,?,?,?)",
                     [(project_id, feature_id, kind, ref, title, body) for kind, ref, title, body in rows])


def set_landed(project_id: str, feature_id: str, sha: str, method: str, confidence: float, detail: str,
               root: Optional[Path] = None) -> None:
    conn = connect(root)
    try:
        conn.execute("INSERT OR REPLACE INTO landed VALUES (?,?,?,?,?,?,?)",
                     (sha, project_id, feature_id, method, confidence, detail, util.now_iso()))
        conn.commit()
    finally:
        conn.close()


def rebuild(root: Optional[Path] = None) -> int:
    """Rebuild the index from every project and feature in the archive."""
    from . import projects

    conn = connect(root)
    count = 0
    try:
        conn.execute("DELETE FROM features")
        conn.execute("DELETE FROM commits")
        conn.execute("DELETE FROM search")
        for pid in projects.list_projects(root):
            features_dir = paths.project_dir(pid, root) / "features"
            if not features_dir.is_dir():
                continue
            for feature_dir in sorted(p for p in features_dir.iterdir() if p.is_dir()):
                feature = records.load_feature(feature_dir)
                evidence = _evidence(pid, feature_dir.name, root)
                _write_feature(conn, pid, feature_dir.name, feature, evidence)
                landed = util.read_json(feature_dir / "evidence" / "landed.json", None)
                if isinstance(landed, dict):
                    for item in landed.get("commits", []):
                        conn.execute("INSERT OR REPLACE INTO landed VALUES (?,?,?,?,?,?,?)",
                                     (item["sha"], pid, feature_dir.name, item.get("method"), item.get("confidence"),
                                      item.get("detail"), item.get("landed_at")))
                count += 1
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('rebuilt_at', ?)", (util.now_iso(),))
        conn.commit()
    finally:
        conn.close()
    return count


def list_features(root: Optional[Path] = None) -> List[dict]:
    conn = connect(root)
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM features ORDER BY updated_at DESC")]
    finally:
        conn.close()


def lookup_commit(sha_prefix: str, root: Optional[Path] = None) -> List[dict]:
    """Features and legs a commit belongs to, by full SHA or a prefix of 7+ characters."""
    prefix = sha_prefix.strip().lower()
    if len(prefix) < 4:
        return []
    conn = connect(root)
    try:
        rows = conn.execute("SELECT * FROM commits WHERE sha LIKE ? ORDER BY committed_at", (prefix + "%",)).fetchall()
        found = [dict(row, match="commit") for row in rows]
        landed = conn.execute("SELECT * FROM landed WHERE sha LIKE ?", (prefix + "%",)).fetchall()
        found += [dict(row, match="landed") for row in landed]
        return found
    finally:
        conn.close()


def search(query: str, limit: int = 60, root: Optional[Path] = None,
           allow: Optional[Callable[[str, str], bool]] = None) -> List[dict]:
    """Rank matches across records; ``allow`` filters inside the query, so the limit counts visible rows."""
    terms = [t.lower() for t in query.split() if t.strip()]
    if not terms:
        return []
    conn = connect(root)
    try:
        clause = " AND ".join(["(lower(s.title) LIKE ? OR lower(s.body) LIKE ? OR lower(s.ref) LIKE ?)"] * len(terms))
        if allow is not None:
            conn.create_function("debrief_allowed", 2, lambda pid, fid: 1 if allow(pid, fid) else 0)
            clause += " AND debrief_allowed(s.project_id, s.feature_id)"
        params: List[str] = []
        for term in terms:
            like = f"%{term}%"
            params += [like, like, like]
        rows = conn.execute(
            f"SELECT s.*, f.title AS feature_title FROM search s LEFT JOIN features f "
            f"ON f.project_id = s.project_id AND f.feature_id = s.feature_id WHERE {clause} LIMIT ?",
            (*params, limit * 3)).fetchall()
    finally:
        conn.close()
    order = {"system": 0, "file": 1, "decision": 2, "test": 3, "brief": 4, "commit": 5, "journal": 6}
    results = []
    for row in rows:
        item = dict(row)
        body = item.pop("body") or ""
        low = body.lower()
        pos = low.find(terms[0])
        item["snippet"] = (body[max(0, pos - 60): pos + 140].replace("\n", " ") if pos >= 0 else body[:160].replace("\n", " "))
        item["score"] = order.get(item["kind"], 9) - (2 if terms[0] in (item.get("title") or "").lower() else 0)
        results.append(item)
    results.sort(key=lambda r: (r["score"], r["feature_id"], r["ref"]))
    return results[:limit]


def dumps(data) -> str:
    return json.dumps(data, indent=2)

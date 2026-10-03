"""Records: load and validate what agents write (protocol ai-sessions/0.1).

Records are Markdown files with YAML frontmatter plus two small structured
files (``tests.yaml`` and ``session.json``). Validation is per file and never
raises: a bad record produces issues, and every other record still loads.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import util
from .vendor import yaml

PROTOCOL = "ai-sessions/0.1"

JOURNAL_KINDS = ("plan", "decision", "finding", "change", "test", "blocker", "handoff")
BRIEF_STATUSES = ("in_progress", "ready_for_review", "merged", "abandoned")
SESSION_STATUSES = ("in_progress", "complete", "blocked", "abandoned")
SYSTEM_CHANGES = ("new", "modified", "touched")
CRITICAL_KINDS = ("loop", "retry", "state-machine", "concurrency", "error-handling", "external-io", "migration")
TEST_KINDS = ("unit", "integration", "e2e", "property", "manual")
TEST_RESULTS = ("pass", "fail", "not_run")
DECISION_STATUSES = ("accepted", "superseded")
REVERSIBILITY = ("cheap", "costly", "one-way")

BRIEF_SECTIONS = ["Intent", "What was built", "Divergences", "Risks and gaps", "Follow-ups"]
SYSTEM_SECTIONS = ["Purpose", "Change", "How it works", "Limitations"]
DECISION_SECTIONS = ["Context", "Options", "Choice and why"]

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def issue(level: str, record: str, message: str) -> dict:
    return {"level": level, "record": record, "message": message}


def jsonable(value: Any) -> Any:
    """Convert YAML-loaded values (dates, sets) into JSON-safe ones."""
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(v) for v in value]
    if isinstance(value, _dt.datetime):
        return util.now_iso(value.astimezone(_dt.timezone.utc)) if value.tzinfo else value.isoformat()
    if isinstance(value, _dt.date):
        return value.isoformat()
    return value


# --- frontmatter and sections -----------------------------------------------

_FRONTMATTER = re.compile(
    r"\A﻿?---[ \t]*\r?\n(?:(.*?)\r?\n)?(?:---|\.\.\.)[ \t]*(?:\r?\n|\Z)", re.S
)


def split_frontmatter(text: str) -> Tuple[Optional[dict], str, Optional[str]]:
    """Return (frontmatter, body, error). Frontmatter is None when absent or invalid."""
    match = _FRONTMATTER.match(text)
    if not match:
        return None, text, None
    raw = match.group(1) or ""
    body = text[match.end():]
    try:
        data = yaml.safe_load(raw) if raw.strip() else {}
    except yaml.YAMLError as exc:
        first = str(exc).strip().splitlines()
        return None, body, "frontmatter is not valid YAML: " + (" ".join(first[:3]) if first else "parse error")
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return None, body, "frontmatter must be a mapping of fields"
    return jsonable(data), body, None


_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")


def section_key(name: str) -> str:
    text = name.lower().replace("&", "and")
    return re.sub(r"[^a-z0-9]+", "", text)


def parse_sections(body: str) -> Tuple[Optional[str], Dict[str, str], List[str]]:
    """Split a Markdown body on level-2 headings.

    Returns (level-1 title, {heading: content}, headings in order). Content
    before the first level-2 heading is stored under the empty heading.
    """
    title: Optional[str] = None
    sections: Dict[str, List[str]] = {"": []}
    order: List[str] = []
    current = ""
    in_fence = False
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
        match = None if in_fence else _HEADING.match(line)
        if match and len(match.group(1)) == 1 and title is None and not order:
            title = match.group(2).strip()
            continue
        if match and len(match.group(1)) == 2:
            current = match.group(2).strip()
            order.append(current)
            sections.setdefault(current, [])
            continue
        sections.setdefault(current, []).append(line)
    result = {k: "\n".join(v).strip("\n") for k, v in sections.items()}
    if not result.get(""):
        result.pop("", None)
    return title, result, order


def find_section(sections: Dict[str, str], name: str) -> Optional[str]:
    wanted = section_key(name)
    for key, value in sections.items():
        if section_key(key) == wanted:
            return value
    return None


# --- anchors ------------------------------------------------------------------

_LINES_RE = re.compile(r"^L?(\d+)(?:\s*[-–:]\s*L?(\d+))?$")


def parse_lines(value: Any) -> Optional[Tuple[int, int]]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return (value, value) if value > 0 else None
    if isinstance(value, (list, tuple)) and value:
        try:
            start = int(value[0])
            end = int(value[-1])
        except (TypeError, ValueError):
            return None
        return (min(start, end), max(start, end)) if start > 0 and end > 0 else None
    match = _LINES_RE.match(str(value).strip())
    if not match:
        return None
    start = int(match.group(1))
    end = int(match.group(2) or start)
    if start <= 0 or end <= 0:
        return None
    return (min(start, end), max(start, end))


def normalize_anchor(raw: Any) -> Tuple[Optional[dict], Optional[str]]:
    """Normalize an anchor to {path, symbol, lines, role}. Returns (anchor, problem)."""
    problem = None
    if isinstance(raw, str):
        text = raw.strip()
        problem = "anchor written as a string; use {path, symbol, lines}"
        match = re.match(r"^(.*?)#L?(\d+(?:-L?\d+)?)$", text)
        if match:
            raw = {"path": match.group(1), "lines": match.group(2)}
        elif ":" in text and not re.match(r"^[A-Za-z]:[\\/]", text):
            path, _, rest = text.partition(":")
            raw = {"path": path, "lines": rest} if parse_lines(rest) else {"path": path, "symbol": rest}
        else:
            raw = {"path": text}
    if not isinstance(raw, dict):
        return None, "anchor must be a mapping with a path"
    path = raw.get("path")
    if not isinstance(path, str) or not path.strip():
        return None, "anchor has no path"
    path = path.strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    symbol = raw.get("symbol")
    symbol = str(symbol).strip() if symbol not in (None, "") else None
    lines_raw = raw.get("lines")
    lines = parse_lines(lines_raw)
    if lines_raw not in (None, "") and lines is None:
        problem = f"anchor lines {lines_raw!r} are not a line or range like \"40-88\""
    role = raw.get("role")
    anchor = {
        "path": path,
        "symbol": symbol,
        "lines": list(lines) if lines else None,
        "role": str(role) if role else None,
    }
    if path.startswith("/"):
        problem = problem or "anchor path is absolute; use a path relative to the repo root"
    return anchor, problem


def anchor_strength(anchor: dict) -> str:
    return "strong" if anchor.get("symbol") or anchor.get("lines") else "weak"


_GLOB_CACHE: Dict[str, "re.Pattern[str]"] = {}


def glob_match(pattern: str, path: str) -> bool:
    """Match a repo path against a glob where ``*`` stays within a directory and ``**`` spans them."""
    if pattern == path:
        return True
    if not any(ch in pattern for ch in "*?["):
        return path.startswith(pattern.rstrip("/") + "/") if pattern.endswith("/") else False
    compiled = _GLOB_CACHE.get(pattern)
    if compiled is None:
        out, i = [], 0
        while i < len(pattern):
            ch = pattern[i]
            if pattern.startswith("**/", i):
                out.append("(?:.*/)?")
                i += 3
            elif pattern.startswith("**", i):
                out.append(".*")
                i += 2
            elif ch == "*":
                out.append("[^/]*")
                i += 1
            elif ch == "?":
                out.append("[^/]")
                i += 1
            else:
                out.append(re.escape(ch))
                i += 1
        compiled = re.compile("^" + "".join(out) + "$")
        _GLOB_CACHE[pattern] = compiled
    return bool(compiled.match(path))


def is_incidental(path: str, patterns: List[str]) -> bool:
    return any(glob_match(p, path) for p in patterns)


# --- loading --------------------------------------------------------------------


def _read_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except UnicodeDecodeError:
        return path.read_bytes().decode("utf-8", "replace")


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _check_id(value: Any, rec: str, what: str, issues: List[dict]) -> None:
    if not isinstance(value, str) or not _ID_RE.match(value):
        issues.append(issue("warning", rec, f"{what} {value!r} should be a kebab-case slug"))


def _text_fields(meta: dict, keys: Tuple[str, ...], rec: str, issues: List[dict]) -> None:
    """Coerce fields that must be text, so a sloppy record (``title: [a, b]``, ``status: yes``)
    is reported and read as text instead of breaking the index or the viewer."""
    for key in keys:
        value = meta.get(key)
        if value is None or isinstance(value, str):
            continue
        if isinstance(value, list):
            text = " ".join(str(v) for v in value)
        elif isinstance(value, bool):
            text = "true" if value else "false"
        else:
            text = str(value)
        meta[key] = text
        issues.append(issue("warning", rec, f"`{key}` should be text; read {value!r} as {text!r}"))


def _check_enum(meta: dict, key: str, allowed, rec: str, issues: List[dict], required: bool = True) -> None:
    value = meta.get(key)
    if value is None:
        if required:
            issues.append(issue("warning", rec, f"missing `{key}` (one of {', '.join(allowed)})"))
        return
    if value not in allowed:
        issues.append(issue("warning", rec, f"`{key}: {value}` is not one of {', '.join(allowed)}"))


def _check_sections(sections: Dict[str, str], wanted: List[str], rec: str, issues: List[dict]) -> None:
    missing = [name for name in wanted if not (find_section(sections, name) or "").strip()]
    if missing:
        issues.append(issue("warning", rec, "missing or empty sections: " + ", ".join(missing)))


def _load_markdown(path: Path, rel: str) -> dict:
    text = _read_text(path) or ""
    meta, body, error = split_frontmatter(text)
    title, sections, order = parse_sections(body)
    issues: List[dict] = []
    if error:
        issues.append(issue("error", rel, error))
    elif meta is None:
        issues.append(issue("error", rel, "no YAML frontmatter (the file must start with ---)"))
    return {
        "path": rel,
        "meta": meta or {},
        "body": body,
        "title_heading": title,
        "sections": sections,
        "section_order": order,
        "issues": issues,
    }


def _normalize_anchor_list(values: Any, rec: str, where: str, issues: List[dict]) -> List[dict]:
    anchors = []
    for raw in _as_list(values):
        anchor, problem = normalize_anchor(raw)
        if problem:
            issues.append(issue("warning", rec, f"{where}: {problem}"))
        if anchor:
            anchors.append(anchor)
    return anchors


def load_brief(feature_dir: Path) -> Optional[dict]:
    path = feature_dir / "brief.md"
    if not path.exists():
        return None
    rec = _load_markdown(path, "brief.md")
    meta, issues = rec["meta"], rec["issues"]
    if rec["meta"] or not issues:
        _text_fields(meta, ("title", "status", "epic", "feature_id"), "brief.md", issues)
        if not meta.get("title"):
            issues.append(issue("warning", "brief.md", "missing `title`"))
        _check_enum(meta, "status", BRIEF_STATUSES, "brief.md", issues)
        meta["sessions"] = [str(s) for s in _as_list(meta.get("sessions"))]
        review = []
        for item in _as_list(meta.get("review_first")):
            if isinstance(item, str):
                item = {"target": item, "why": ""}
            if not isinstance(item, dict) or not item.get("target"):
                issues.append(issue("warning", "brief.md", "review_first entries need a `target`"))
                continue
            review.append({"target": str(item.get("target")), "why": str(item.get("why") or "")})
        if len(review) > 3:
            issues.append(issue("warning", "brief.md", "review_first should name at most three targets"))
        meta["review_first"] = review
        incidental = []
        for item in _as_list(meta.get("incidental")):
            if isinstance(item, dict):
                item = item.get("path")
            if isinstance(item, str) and item.strip():
                cleaned = item.strip().replace("\\", "/")
                while cleaned.startswith("./"):
                    cleaned = cleaned[2:]
                incidental.append(cleaned)
        meta["incidental"] = incidental
        if meta.get("epic") is not None and not _ID_RE.match(meta["epic"]):
            issues.append(issue("warning", "brief.md", "`epic` should be a slug"))
        _check_sections(rec["sections"], BRIEF_SECTIONS, "brief.md", issues)
    return rec


def load_system(path: Path, feature_dir: Path) -> dict:
    rel = str(path.relative_to(feature_dir))
    rec = _load_markdown(path, rel)
    meta, issues = rec["meta"], rec["issues"]
    _text_fields(meta, ("title", "change"), rel, issues)
    stem = path.stem
    if not meta.get("id"):
        meta["id"] = stem
    elif meta.get("id") != stem:
        issues.append(issue("warning", rel, f"id `{meta.get('id')}` differs from the file name `{stem}`"))
    meta["id"] = str(meta["id"])
    _check_id(meta["id"], rel, "system id", issues)
    if not meta.get("title"):
        issues.append(issue("warning", rel, "missing `title`"))
        meta["title"] = meta["id"]
    _check_enum(meta, "change", SYSTEM_CHANGES, rel, issues)
    deps = []
    for item in _as_list(meta.get("depends_on")):
        if isinstance(item, str):
            item = {"system": item, "relation": ""}
        if not isinstance(item, dict) or not item.get("system"):
            issues.append(issue("warning", rel, "depends_on entries need a `system`"))
            continue
        deps.append({"system": str(item["system"]), "relation": str(item.get("relation") or "")})
    meta["depends_on"] = deps
    meta["anchors"] = _normalize_anchor_list(meta.get("anchors"), rel, "anchors", issues)
    if not meta["anchors"]:
        issues.append(issue("warning", rel, "no anchors: a system must point at its code"))
    elif not any(a.get("symbol") or a.get("lines") for a in meta["anchors"]):
        issues.append(issue("warning", rel, "only path anchors: add a symbol or line range"))
    paths_out = []
    for item in _as_list(meta.get("critical_paths")):
        if not isinstance(item, dict):
            issues.append(issue("warning", rel, "critical_paths entries must be mappings"))
            continue
        cp_id = str(item.get("id") or "")
        if not cp_id:
            issues.append(issue("warning", rel, "a critical path has no `id`"))
            continue
        _check_id(cp_id, rel, "critical path id", issues)
        _text_fields(item, ("kind",), rel, issues)
        kind = item.get("kind")
        if kind not in CRITICAL_KINDS:
            issues.append(issue("warning", rel, f"critical path `{cp_id}` kind {kind!r} is not one of {', '.join(CRITICAL_KINDS)}"))
        anchor, problem = normalize_anchor(item.get("anchor")) if item.get("anchor") is not None else (None, "has no anchor")
        if problem:
            issues.append(issue("warning", rel, f"critical path `{cp_id}`: {problem}"))
        if not item.get("invariant"):
            issues.append(issue("warning", rel, f"critical path `{cp_id}` has no invariant"))
        paths_out.append({
            "id": cp_id,
            "kind": kind,
            "anchor": anchor,
            "invariant": str(item.get("invariant") or ""),
            "failure_mode": str(item.get("failure_mode") or ""),
        })
    meta["critical_paths"] = paths_out
    meta["decisions"] = [str(d) for d in _as_list(meta.get("decisions"))]
    _check_sections(rec["sections"], SYSTEM_SECTIONS, rel, issues)
    return rec


def load_tests(feature_dir: Path) -> Optional[dict]:
    path = feature_dir / "tests.yaml"
    if not path.exists():
        alt = feature_dir / "tests.yml"
        if not alt.exists():
            return None
        path = alt
    rel = path.name
    issues: List[dict] = []
    text = _read_text(path) or ""
    try:
        data = yaml.safe_load(text) if text.strip() else {}
    except yaml.YAMLError as exc:
        first = " ".join(str(exc).strip().splitlines()[:3])
        return {"path": rel, "tests": [], "gaps": [], "issues": [issue("error", rel, f"not valid YAML: {first}")]}
    data = jsonable(data)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return {"path": rel, "tests": [], "gaps": [], "issues": [issue("error", rel, "must be a mapping with `tests` and `gaps`")]}
    tests = []
    seen = set()
    for item in _as_list(data.get("tests")):
        if not isinstance(item, dict) or not item.get("id"):
            issues.append(issue("warning", rel, "every test needs an `id`"))
            continue
        tid = str(item["id"])
        if tid in seen:
            issues.append(issue("warning", rel, f"duplicate test id `{tid}`"))
        seen.add(tid)
        _check_id(tid, rel, "test id", issues)
        _text_fields(item, ("kind", "claimed_result"), rel, issues)
        validates = [str(v) for v in _as_list(item.get("validates"))]
        if not validates:
            issues.append(issue("warning", rel, f"test `{tid}` validates nothing"))
        if not item.get("claim"):
            issues.append(issue("warning", rel, f"test `{tid}` has no claim"))
        kind = item.get("kind")
        if kind not in TEST_KINDS:
            issues.append(issue("warning", rel, f"test `{tid}` kind {kind!r} is not one of {', '.join(TEST_KINDS)}"))
        result = item.get("claimed_result")
        if result not in TEST_RESULTS:
            issues.append(issue("warning", rel, f"test `{tid}` claimed_result {result!r} is not one of {', '.join(TEST_RESULTS)}"))
        if not item.get("command") and kind != "manual":
            issues.append(issue("warning", rel, f"test `{tid}` has no command"))
        tests.append({
            "id": tid,
            "validates": validates,
            "claim": str(item.get("claim") or ""),
            "kind": kind,
            "anchors": _normalize_anchor_list(item.get("anchors"), rel, f"test `{tid}` anchors", issues),
            "command": str(item.get("command") or ""),
            "claimed_result": result,
        })
    if "gaps" not in data:
        issues.append(issue("warning", rel, "`gaps` is required, even as an empty list"))
    gaps = []
    for item in _as_list(data.get("gaps")):
        if isinstance(item, str):
            item = {"validates": "", "note": item}
        if not isinstance(item, dict):
            continue
        gaps.append({"validates": str(item.get("validates") or ""), "note": str(item.get("note") or "")})
    return {"path": rel, "tests": tests, "gaps": gaps, "issues": issues}


def load_decision(path: Path, feature_dir: Path) -> dict:
    rel = str(path.relative_to(feature_dir))
    rec = _load_markdown(path, rel)
    meta, issues = rec["meta"], rec["issues"]
    _text_fields(meta, ("title", "status", "reversibility"), rel, issues)
    stem = path.stem
    meta["id"] = str(meta.get("id") or stem)
    if meta["id"] != stem:
        issues.append(issue("warning", rel, f"id `{meta['id']}` differs from the file name `{stem}`"))
    if not re.match(r"^\d{3}-", stem):
        issues.append(issue("warning", rel, "decision files are named <nnn>-<slug>.md"))
    if not meta.get("title"):
        issues.append(issue("warning", rel, "missing `title`"))
        meta["title"] = meta["id"]
    _check_enum(meta, "status", DECISION_STATUSES, rel, issues)
    _check_enum(meta, "reversibility", REVERSIBILITY, rel, issues)
    meta["systems"] = [str(s) for s in _as_list(meta.get("systems"))]
    _check_sections(rec["sections"], DECISION_SECTIONS, rel, issues)
    return rec


_ENTRY_RE = re.compile(r"^###[ \t]+(\S+)[ \t]*(?:·|•|-|—|–|\|)[ \t]*([A-Za-z_-]+)[ \t]*$")


def parse_journal(text: str, rel: str = "journal.md") -> Tuple[List[dict], List[dict]]:
    entries: List[dict] = []
    issues: List[dict] = []
    current: Optional[dict] = None
    lines: List[str] = []
    for number, line in enumerate(text.splitlines(), 1):
        match = _ENTRY_RE.match(line)
        if match:
            if current is not None:
                current["text"] = "\n".join(lines).strip()
                entries.append(current)
            stamp, kind = match.group(1), match.group(2).lower()
            if util.parse_iso(stamp) is None:
                issues.append(issue("warning", rel, f"line {number}: `{stamp}` is not a UTC time like 2026-10-02T19:41Z"))
            if kind not in JOURNAL_KINDS:
                issues.append(issue("warning", rel, f"line {number}: unknown entry kind `{kind}`"))
            current = {"at": stamp, "kind": kind, "line": number}
            lines = []
        elif line.startswith("### ") and current is None and not entries:
            issues.append(issue("warning", rel, f"line {number}: heading is not `### <time> · <kind>`"))
        elif current is not None:
            lines.append(line)
    if current is not None:
        current["text"] = "\n".join(lines).strip()
        entries.append(current)
    return entries, issues


def load_session(session_dir: Path, feature_dir: Path) -> dict:
    rel_dir = str(session_dir.relative_to(feature_dir))
    issues: List[dict] = []
    meta = util.read_json(session_dir / "session.json", None)
    if not isinstance(meta, dict):
        issues.append(issue("error", f"{rel_dir}/session.json", "missing or not valid JSON"))
        meta = {"session_id": session_dir.name}
    if meta.get("status") not in SESSION_STATUSES:
        issues.append(issue("warning", f"{rel_dir}/session.json", f"status {meta.get('status')!r} is not one of {', '.join(SESSION_STATUSES)}"))
    journal_text = _read_text(session_dir / "journal.md") or ""
    entries, journal_issues = parse_journal(journal_text, f"{rel_dir}/journal.md")
    issues.extend(journal_issues)
    if entries and entries[0]["kind"] != "plan":
        issues.append(issue("warning", f"{rel_dir}/journal.md", "a session opens with a `plan` entry"))
    if meta.get("status") in ("complete", "blocked", "abandoned") and not any(e["kind"] == "handoff" for e in entries):
        issues.append(issue("warning", f"{rel_dir}/journal.md", "the session ended without a `handoff` entry"))
    runs = []
    runs_dir = session_dir / "runs"
    if runs_dir.is_dir():
        for run_path in sorted(runs_dir.glob("*.json")):
            run = util.read_json(run_path, None)
            if isinstance(run, dict):
                run["file"] = f"{rel_dir}/runs/{run_path.name}"
                runs.append(run)
    return {"meta": meta, "journal": entries, "runs": runs, "issues": issues, "path": rel_dir}


def load_legs(feature_dir: Path) -> List[dict]:
    legs = []
    legs_dir = feature_dir / "legs"
    if legs_dir.is_dir():
        for path in sorted(legs_dir.glob("leg-*.json")):
            data = util.read_json(path, None)
            if isinstance(data, dict):
                data.setdefault("leg_id", path.stem)
                legs.append(data)
    legs.sort(key=lambda leg: leg_number(leg.get("leg_id", "")))
    return legs


def leg_number(leg_id: str) -> int:
    match = re.search(r"(\d+)$", leg_id or "")
    return int(match.group(1)) if match else 0


def load_feature(feature_dir: Path) -> dict:
    """Load every record of a feature. Never raises on bad content."""
    feature_dir = Path(feature_dir)
    issues: List[dict] = []
    brief = load_brief(feature_dir)
    systems = []
    systems_dir = feature_dir / "systems"
    if systems_dir.is_dir():
        for path in sorted(systems_dir.glob("*.md")):
            systems.append(load_system(path, feature_dir))
    tests = load_tests(feature_dir)
    decisions = []
    decisions_dir = feature_dir / "decisions"
    if decisions_dir.is_dir():
        for path in sorted(decisions_dir.glob("*.md")):
            decisions.append(load_decision(path, feature_dir))
    sessions = []
    sessions_dir = feature_dir / "sessions"
    if sessions_dir.is_dir():
        for path in sorted(p for p in sessions_dir.iterdir() if p.is_dir()):
            sessions.append(load_session(path, feature_dir))
    sessions.sort(key=lambda s: str(s["meta"].get("started_at") or s["meta"].get("session_id") or ""))
    legs = load_legs(feature_dir)
    feature = {
        "feature_id": feature_dir.name,
        "project_id": feature_dir.parent.parent.name,
        "path": str(feature_dir),
        "brief": brief,
        "systems": systems,
        "tests": tests,
        "decisions": decisions,
        "sessions": sessions,
        "legs": legs,
        "issues": issues,
    }
    issues.extend(cross_check(feature))
    return feature


def cross_check(feature: dict) -> List[dict]:
    """Checks that span records: references must resolve."""
    issues: List[dict] = []
    system_ids = {s["meta"]["id"] for s in feature["systems"]}
    paths_by_system = {s["meta"]["id"]: {cp["id"] for cp in s["meta"]["critical_paths"]} for s in feature["systems"]}
    decision_ids = {d["meta"]["id"] for d in feature["decisions"]}

    def target_ok(target: str) -> bool:
        system, _, cp = target.partition("/")
        if system not in system_ids:
            return False
        return not cp or cp in paths_by_system.get(system, set())

    for system in feature["systems"]:
        rel = system["path"]
        for dep in system["meta"]["depends_on"]:
            if dep["system"] not in system_ids:
                system["issues"].append(issue("warning", rel, f"depends_on names unknown system `{dep['system']}`"))
        for dec in system["meta"]["decisions"]:
            if dec not in decision_ids:
                system["issues"].append(issue("warning", rel, f"names unknown decision `{dec}`"))
    tests = feature.get("tests")
    if tests:
        for test in tests["tests"]:
            for target in test["validates"]:
                if not target_ok(target):
                    tests["issues"].append(issue("warning", tests["path"], f"test `{test['id']}` validates unknown `{target}`"))
        for gap in tests["gaps"]:
            if gap["validates"] and not target_ok(gap["validates"]):
                tests["issues"].append(issue("warning", tests["path"], f"gap names unknown `{gap['validates']}`"))
    brief = feature.get("brief")
    if brief:
        for item in brief["meta"].get("review_first", []):
            target = item["target"]
            if not (target_ok(target) or target in {f"tests/{t['id']}" for t in (tests or {}).get("tests", [])}
                    or target in decision_ids):
                brief["issues"].append(issue("warning", "brief.md", f"review_first target `{target}` is not a system, critical path or decision"))
        known_sessions = {s["meta"].get("session_id") for s in feature["sessions"]}
        for sid in brief["meta"].get("sessions", []):
            if sid not in known_sessions:
                brief["issues"].append(issue("warning", "brief.md", f"lists unknown session `{sid}`"))
        fid = brief["meta"].get("feature_id")
        if fid and fid != feature["feature_id"]:
            brief["issues"].append(issue("warning", "brief.md", f"feature_id `{fid}` differs from the directory `{feature['feature_id']}`"))
    for decision in feature["decisions"]:
        for sid in decision["meta"]["systems"]:
            if sid not in system_ids:
                decision["issues"].append(issue("warning", decision["path"], f"names unknown system `{sid}`"))
    return issues


def all_issues(feature: dict) -> List[dict]:
    found = list(feature.get("issues", []))
    if feature.get("brief"):
        found.extend(feature["brief"]["issues"])
    for group in ("systems", "decisions", "sessions"):
        for rec in feature.get(group, []):
            found.extend(rec["issues"])
    if feature.get("tests"):
        found.extend(feature["tests"]["issues"])
    return found


def closeout_issues(feature: dict) -> List[dict]:
    """What a finished closeout must contain, as errors."""
    problems = []
    if not feature.get("brief"):
        problems.append(issue("error", "brief.md", "missing: write the brief (closeout step 5)"))
    if not feature.get("systems"):
        problems.append(issue("error", "systems/", "no system files: describe each mechanism (closeout step 2)"))
    if feature.get("tests") is None:
        problems.append(issue("error", "tests.yaml", "missing: list test groups and gaps (closeout step 3)"))
    return problems


def dumps(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)

"""Flag rules: language-agnostic regexes over added and removed diff lines.

The defaults ship in ``bundle/rules/default.json``. A project can tune them in
``<archive>/projects/<id>/rules.json`` (never in the repo)::

    {"disable": ["todo"], "severity": {"retry": "medium"}, "rules": [{...new or replacing rules...}]}

Every rule has an id, a category (``critical``, ``risky`` or ``test``), a
severity and a message. Critical rules flag only constructs that no declared
critical path covers.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from . import resources
from .records import glob_match

SEVERITIES = ("high", "medium", "low")


class Rule:
    def __init__(self, data: dict):
        self.id = str(data["id"])
        self.category = data.get("category", "risky")
        self.kind = data.get("kind")
        self.severity = data.get("severity", "low") if data.get("severity") in SEVERITIES else "low"
        self.on = data.get("on", "added")
        self.pattern = re.compile(data["pattern"])
        self.message = data.get("message", self.id)
        self.paths = list(data.get("paths") or [])
        self.exclude = list(data.get("exclude") or [])
        self.once_per_file = bool(data.get("once_per_file"))
        self.code_only = bool(data.get("code_only"))

    def applies_to(self, path: str) -> bool:
        if self.paths and not any(glob_match(p, path) for p in self.paths):
            return False
        return not any(glob_match(p, path) for p in self.exclude)


_STRING = re.compile(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|`(?:[^`\\]|\\.)*`')
_COMMENT = re.compile(r"(?:^|\s)(?:#|//).*$")


def code_text(line: str) -> str:
    """A line with string contents and trailing comments removed."""
    return _COMMENT.sub("", _STRING.sub('""', line))


def load(project_dir: Optional[Path] = None) -> List[Rule]:
    data = json.loads(resources.read_text("bundle/rules/default.json"))
    rules: Dict[str, dict] = {r["id"]: r for r in data["rules"]}
    if project_dir is not None:
        override_path = Path(project_dir) / "rules.json"
        try:
            override = json.loads(override_path.read_text(encoding="utf-8")) if override_path.exists() else {}
        except ValueError:
            override = {}
        for rid in override.get("disable", []) or []:
            rules.pop(rid, None)
        for rid, severity in (override.get("severity") or {}).items():
            if rid in rules and severity in SEVERITIES:
                rules[rid] = dict(rules[rid], severity=severity)
        for rule in override.get("rules", []) or []:
            if isinstance(rule, dict) and rule.get("id") and rule.get("pattern"):
                rules[rule["id"]] = rule
    compiled = []
    for rule in rules.values():
        try:
            compiled.append(Rule(rule))
        except (re.error, KeyError, TypeError):
            continue
    return compiled


def scan(rules: Iterable[Rule], path: str, hunks) -> List[dict]:
    """Flags for one file's hunks (diffparse.Hunk objects)."""
    flags: List[dict] = []
    applicable = [r for r in rules if r.applies_to(path)]
    if not applicable:
        return flags
    fired_once = set()
    for hunk in hunks:
        for tag, old_line, new_line, text in hunk.numbered():
            if tag not in "+-":
                continue
            for rule in applicable:
                if rule.on == "added" and tag != "+":
                    continue
                if rule.on == "removed" and tag != "-":
                    continue
                if rule.once_per_file and rule.id in fired_once:
                    continue
                subject = code_text(text) if rule.code_only else text
                if not rule.pattern.search(subject):
                    continue
                fired_once.add(rule.id)
                flags.append({
                    "rule": rule.id,
                    "category": rule.category,
                    "kind": rule.kind,
                    "severity": rule.severity,
                    "message": rule.message,
                    "path": path,
                    "side": "new" if tag == "+" else "old",
                    "line": new_line if tag == "+" else old_line,
                    "text": text.strip()[:200],
                    "hunk_id": hunk.id,
                })
    return flags

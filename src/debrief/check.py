"""``debrief check``: validate records and print what to fix."""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

from . import paths, records


def feature_dirs_for(path: Optional[str]) -> List[Path]:
    """Resolve a feature dir, project dir, archive or repo path to feature dirs."""
    target = Path(path or ".").expanduser().resolve()
    if (target / "legs").is_dir() or (target / "sessions").is_dir() or (target / "brief.md").exists():
        return [target]
    if (target / "features").is_dir():
        return sorted(p for p in (target / "features").iterdir() if p.is_dir())
    if (target / "projects").is_dir():
        found = []
        for project in sorted((target / "projects").iterdir()):
            if (project / "features").is_dir():
                found.extend(sorted(p for p in (project / "features").iterdir() if p.is_dir()))
        return found
    from .session import Context, Untracked

    try:
        ctx = Context(target)
    except Untracked:
        return []
    return [ctx.feature_dir] if ctx.feature_dir.exists() else []


def run(path: Optional[str], closeout: bool = False, as_json: bool = False) -> int:
    dirs = feature_dirs_for(path)
    if not dirs:
        print("No records found here. Pass a feature directory, a project directory or a tracked repo.")
        return 1
    results = []
    errors = 0
    for feature_dir in dirs:
        feature = records.load_feature(feature_dir)
        found = records.all_issues(feature)
        if closeout:
            found = records.closeout_issues(feature) + found
        errors += sum(1 for item in found if item["level"] == "error")
        results.append({"feature_dir": str(feature_dir), "issues": found})
    if as_json:
        print(json.dumps({"results": results, "errors": errors}, indent=2))
        return 1 if errors else 0
    for result in results:
        rel = result["feature_dir"]
        try:
            rel = str(Path(rel).relative_to(paths.archive_root()))
        except ValueError:
            pass
        found = result["issues"]
        if not found:
            print(f"{rel}: ok")
            continue
        print(f"{rel}: {len(found)} issue(s)")
        for item in found:
            print(f"  {item['level']:7} {item['record']}: {item['message']}")
    return 1 if errors else 0

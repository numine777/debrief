"""``debrief sync``: commit, pull and push project archive repositories."""

from __future__ import annotations

from pathlib import Path

from . import archive, paths, projects, residency


def cli(args) -> int:
    root = paths.archive_root()
    if args.project:
        ids = [args.project]
    else:
        found = projects.find_project(Path.cwd())
        ids = [found[0]] if found and args.remote else projects.list_projects(root)
    if args.remote and len(ids) != 1:
        print("Pass the project whose archive remote to set (or run inside its repo).")
        return 1
    status = 0
    for pid in ids:
        project_dir = paths.project_dir(pid, root)
        if not archive.is_repo(project_dir):
            print(f"{pid}: no archive repository (run `debrief init` in its repo)")
            status = 1
            continue
        try:
            if args.remote:
                archive.set_remote(project_dir, args.remote)
                print(f"{pid}: archive remote set to {args.remote}")
            result = archive.sync(project_dir, "Sync records")
        except residency.ResidencyError as exc:
            print(f"{pid}: {exc}")
            status = 1
            continue
        parts = []
        if result.get("committed"):
            parts.append("committed local records")
        if result.get("pulled"):
            parts.append("pulled")
        elif result.get("reason") and result.get("reason") != "no remote":
            parts.append(f"not pulled ({result['reason']})")
        if result.get("conflicts"):
            parts.append("kept both versions of " + ", ".join(result["conflicts"]))
        if result.get("pushed"):
            parts.append("pushed")
        if not archive.remote_url(project_dir):
            parts.append("no remote: records stay on this host")
        print(f"{pid}: " + (", ".join(parts) or "up to date"))
    return status

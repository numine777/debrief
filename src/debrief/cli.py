"""The ``debrief`` command line."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from . import __version__

DESCRIPTION = """\
Debrief: review coding-agent work through the records agents write, checked
against git and served by an offline viewer.

Agent side:   debrief install, debrief init <repo>; agents run debrief-session.
Review side:  debrief serve, then open the printed URL.
"""


def _print(lines: List[str]) -> None:
    for line in lines:
        print(line)


def cmd_install(args) -> int:
    from . import install

    harnesses = None if not args.harness else [h for part in args.harness for h in part.split(",") if h]
    if harnesses:
        unknown = [h for h in harnesses if h not in install.HARNESSES + ("none",)]
        if unknown:
            print(f"Unknown harness: {', '.join(unknown)}. Choose from {', '.join(install.HARNESSES)} or none.")
            return 2
    _print(install.run_install(harnesses, claude_hooks=args.claude_settings, dry_run=args.dry_run))
    return 0


def cmd_uninstall(args) -> int:
    from . import install

    _print(install.run_uninstall(dry_run=args.dry_run))
    return 0


def cmd_init(args) -> int:
    from . import config, projects, residency

    config.ensure_default()
    try:
        result = projects.init_project(Path(args.repo), remote=args.remote, name=args.name)
    except (ValueError, residency.ResidencyError) as exc:
        print(f"debrief init: {exc}")
        return 1
    verb = "Registered" if result.created else "Updated"
    print(f"{verb} {result.name} as project {result.project_id}.")
    print(f"  archive  {result.project_dir}")
    print("Nothing was written into the repository. Agents on this host now keep records for it.")
    return 0


def cmd_projects(args) -> int:
    from . import projects

    registry = projects.registered_projects()
    ids = sorted(set(projects.list_projects()) | set(registry))
    if not ids:
        print("No projects yet. Track a repo with `debrief init <repo>`.")
        return 0
    for pid in ids:
        meta = projects.project_meta(pid)
        dirs = registry.get(pid, {}).get("common_dirs", [])
        where = ", ".join(dirs) if dirs else "not on this host"
        print(f"{pid}  {meta.get('display_name', '')}  [{where}]")
    return 0


def cmd_check(args) -> int:
    from . import check

    return check.run(args.path, closeout=args.closeout, as_json=args.json)


def cmd_config(args) -> int:
    from . import config

    path = config.ensure_default()
    print(path)
    if args.show:
        print(path.read_text(encoding="utf-8"))
    return 0


def cmd_ingest(args) -> int:
    from . import ingest

    return ingest.cli(args)


def cmd_serve(args) -> int:
    from . import server

    return server.cli(args)


def cmd_hub(args) -> int:
    from . import hub

    return hub.cli(args)


def cmd_sync(args) -> int:
    from . import syncing

    return syncing.cli(args)


def cmd_show(args) -> int:
    from . import squash

    return squash.cli_show(args)


def cmd_squash(args) -> int:
    from . import squash

    return squash.cli_squash(args)


def cmd_export(args) -> int:
    from . import export

    return export.cli(args)


def cmd_request_close(args) -> int:
    from . import review

    return review.cli_request_close(args)


def cmd_service(args) -> int:
    from . import service

    return service.cli(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="debrief", description=DESCRIPTION,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"debrief {__version__}")
    sub = parser.add_subparsers(dest="cmd", metavar="command")

    p = sub.add_parser("install", help="install agent instructions, skills and launchers on this host")
    p.add_argument("--harness", action="append",
                   help="devin, claude, codex, pi or none (repeatable; default: detect)")
    p.add_argument("--claude-settings", action="store_true",
                   help="also add a SessionStart hook, archive access and allow rules to ~/.claude/settings.json")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("uninstall", help="remove what install wrote (keeps the archive)")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_uninstall)

    p = sub.add_parser("init", help="track a repo; writes nothing into it")
    p.add_argument("repo", nargs="?", default=".")
    p.add_argument("--remote", help="git remote for this project's archive repository")
    p.add_argument("--name", help="display name")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("projects", help="list tracked projects")
    p.set_defaults(func=cmd_projects)

    sub.add_parser("session", help="the agent-side commands (same as debrief-session)")

    p = sub.add_parser("check", help="validate records")
    p.add_argument("path", nargs="?", help="feature dir, project dir, archive or repo (default: here)")
    p.add_argument("--closeout", action="store_true", help="also require brief, systems and tests")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("config", help="print the config path, creating a default config")
    p.add_argument("--show", action="store_true")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("ingest", help="validate records and compute evidence")
    p.add_argument("repos", nargs="*", help="repos to ingest (default: every tracked project)")
    p.add_argument("--project", help="project id")
    p.add_argument("--feature", help="feature id")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("serve", help="serve the viewer on 127.0.0.1")
    p.add_argument("--port", type=int)
    p.add_argument("--watch", action="store_true", help="ingest and sync as records and repos change")
    p.add_argument("--no-ingest", action="store_true", help="skip the ingest at startup")
    p.add_argument("--open", action="store_true", help="open a browser")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("hub", help="run or administer the shared hub")
    p.add_argument("hub_cmd", nargs="?", default="serve",
                   choices=["serve", "init", "adduser", "token", "grant", "revoke", "users", "repo"])
    p.add_argument("args", nargs="*")
    p.add_argument("--dir", help="hub data directory")
    p.add_argument("--bind", help="address to listen on")
    p.add_argument("--port", type=int)
    p.add_argument("--cert")
    p.add_argument("--key")
    p.add_argument("--role", choices=["owner", "reviewer", "reader"])
    p.add_argument("--insecure-http", action="store_true", help="serve without TLS (testing only)")
    p.add_argument("--admin", action="store_true", help="with adduser: the user can see every project")
    p.set_defaults(func=cmd_hub)

    p = sub.add_parser("sync", help="commit, pull and push project archives")
    p.add_argument("project", nargs="?")
    p.add_argument("--remote", help="set this project's archive remote first")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("show", help="show the feature, leg and systems behind a commit")
    p.add_argument("sha")
    p.add_argument("--repo", help="repo to resolve the sha in (default: here)")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("squash", help="compact a feature's records into one consolidated record")
    p.add_argument("feature")
    p.add_argument("--project")
    p.add_argument("--model", action="store_true", help="also run experimental model compaction")
    p.set_defaults(func=cmd_squash)

    p = sub.add_parser("export", help="write a feature as one self-contained HTML file")
    p.add_argument("feature")
    p.add_argument("--project")
    p.add_argument("-o", "--output", default=None)
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("request-close", help="ask the agent to close out the open leg")
    p.add_argument("feature", nargs="?")
    p.add_argument("--project")
    p.add_argument("--note")
    p.set_defaults(func=cmd_request_close)

    p = sub.add_parser("service", help="install the viewer as a user service")
    p.add_argument("action", choices=["install", "uninstall", "print"])
    p.set_defaults(func=cmd_service)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "session":
        from . import session

        return session.main(argv[1:])
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

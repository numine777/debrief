"""``bin/session``: the agent-side commands, installed as ``debrief-session``.

    start     open a session (and a leg if none is open) for the current branch
    context   print the session state without changing anything (for hooks)
    now       print the journal time stamp, log new commits, relay requests
    run       run a command and record it in the session's run ledger
    changed   list what the feature changed and what explains each file
    close     end the session with a status
    publish   close out the leg: ingest, commit records to the archive, sync

Everything here reads the project repository and writes only to the archive.
"""

from __future__ import annotations

import argparse
import collections
import os
import secrets
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import archive, gitutil, paths, projects, records, util
from .config import load as load_config

CLOSEOUT_SKILL = "ai-session-closeout/SKILL.md"
SESSION_SKILL = "ai-session/SKILL.md"
OUTPUT_TAIL_LINES = 60
OUTPUT_TAIL_CHARS = 6000
MAX_DIRTY_FILES = 500
AGENT_NETWORK_TIMEOUT = 20  # seconds for the pulls start and now make; they retry on the next call
PUBLISH_NETWORK_TIMEOUT = 60


def skills_dir() -> Path:
    return Path.home() / ".agents" / "skills"


class Untracked(Exception):
    """The working directory is not a git repo registered with Debrief."""


class Context:
    """Where the current working directory's records live."""

    def __init__(self, cwd: Path, feature_override: Optional[str] = None):
        self.cwd = cwd
        top = gitutil.toplevel(cwd)
        if top is None:
            raise Untracked("Not inside a git repository, so no session records are needed here.")
        self.repo_root = top.resolve()
        found = projects.find_project(self.repo_root)
        if found is None:
            raise Untracked(
                "This repo isn't tracked by Debrief, so skip session records here. "
                "(A developer can track it with `debrief init`.)"
            )
        self.project_id, self.registry_entry = found
        self.archive_root = paths.archive_root()
        self.project_dir = paths.project_dir(self.project_id)
        self.branch = gitutil.current_branch(self.repo_root)
        self.head = gitutil.head(self.repo_root)
        if feature_override:
            self.feature_id = util.feature_id_for_branch(feature_override)
        elif self.branch:
            self.feature_id = self._branch_feature(self.branch)
        else:
            self.feature_id = self._detached_feature()
        self.feature_dir = paths.feature_dir(self.project_id, self.feature_id)

    def _branch_feature(self, branch: str) -> str:
        """The branch's feature id; branches whose names slug alike (a+b, a@b) get distinct ids."""
        fid = util.feature_id_for_branch(branch)
        legs = records.load_legs(paths.feature_dir(self.project_id, fid))
        owner = next((leg.get("branch") for leg in legs if leg.get("branch")), None)
        if owner and owner != branch:
            return f"{fid}-{util.short_hash(branch, 6)}"
        return fid

    def _detached_feature(self) -> str:
        """A detached HEAD keeps the feature its session started with, even as commits move HEAD."""
        entry = _load_state()["sessions"].get(str(self.repo_root)) or {}
        if entry.get("project_id") == self.project_id and str(entry.get("feature_id", "")).startswith("detached-"):
            return entry["feature_id"]
        return f"detached-{(self.head or 'unborn')[:8]}"

    @property
    def legs_dir(self) -> Path:
        return self.feature_dir / "legs"

    def lock(self):
        return util.file_lock(self.feature_dir / ".feature.lock")


# --- host-local state -------------------------------------------------------------


def _state_path() -> Path:
    return paths.archive_root() / ".state" / "current.json"


def _load_state() -> dict:
    data = util.read_json(_state_path(), None)
    if not isinstance(data, dict):
        data = {}
    data.setdefault("sessions", {})
    data.setdefault("pulls", {})
    return data


def _save_state(data: dict) -> None:
    util.write_json(_state_path(), data)


def _state_lock():
    return util.file_lock(paths.archive_root() / ".state" / "state.lock")


def current_session_id(ctx: Context) -> Optional[str]:
    entry = _load_state()["sessions"].get(str(ctx.repo_root))
    if not entry:
        return None
    if entry.get("project_id") != ctx.project_id or entry.get("feature_id") != ctx.feature_id:
        return None
    if not (ctx.feature_dir / "sessions" / entry.get("session_id", "") / "session.json").exists():
        return None
    return entry.get("session_id")


def current_entry(ctx: Context) -> Optional[dict]:
    return _load_state()["sessions"].get(str(ctx.repo_root))


def _set_current(ctx: Context, session_id: Optional[str]) -> None:
    with _state_lock():
        data = _load_state()
        key = str(ctx.repo_root)
        if session_id:
            data["sessions"][key] = {
                "project_id": ctx.project_id,
                "feature_id": ctx.feature_id,
                "session_id": session_id,
                "since": util.now_iso(),
            }
        else:
            data["sessions"].pop(key, None)
        _save_state(data)


# --- legs ----------------------------------------------------------------------------


def leg_path(ctx: Context, leg_id: str) -> Path:
    return ctx.legs_dir / f"{leg_id}.json"


def open_leg(ctx: Context) -> Optional[dict]:
    legs = records.load_legs(ctx.feature_dir)
    if legs and not legs[-1].get("closed_at"):
        return legs[-1]
    return None


def last_leg(ctx: Context) -> Optional[dict]:
    legs = records.load_legs(ctx.feature_dir)
    return legs[-1] if legs else None


def save_leg(ctx: Context, leg: dict) -> None:
    util.write_json(leg_path(ctx, leg["leg_id"]), leg)


def _initial_base(ctx: Context) -> Optional[str]:
    head = ctx.head
    if not head:
        return None
    default = gitutil.default_branch(ctx.repo_root)
    ref = gitutil.default_branch_ref(ctx.repo_root)
    if ref and ctx.branch and ctx.branch != default:
        base = gitutil.merge_base(ctx.repo_root, ref, head)
        if base:
            return base
    return head


def ensure_open_leg(ctx: Context) -> Tuple[dict, bool]:
    """Return the open leg, creating the next one if every leg is closed."""
    legs = records.load_legs(ctx.feature_dir)
    if legs and not legs[-1].get("closed_at"):
        return legs[-1], False
    previous = legs[-1] if legs else None
    number = records.leg_number(previous["leg_id"]) + 1 if previous else 1
    base = None
    if previous:
        prev_head = previous.get("head_commit") or previous.get("head_ref")
        if prev_head and prev_head != "WORKTREE" and ctx.head and gitutil.is_ancestor(ctx.repo_root, prev_head, ctx.head):
            base = prev_head
    if base is None:
        base = _initial_base(ctx)
    leg = {
        "leg_id": f"leg-{number:02d}",
        "project_id": ctx.project_id,
        "feature_id": ctx.feature_id,
        "branch": ctx.branch,
        "opened_at": util.now_iso(),
        "base_ref": base,
        "head_ref": None,
        "head_commit": None,
        "head_tree": None,
        "sessions": [],
        "commits": [],
        "close_requested_at": None,
        "close_requested_by": None,
        "closed_at": None,
        "closed_by": None,
        "feedback_relayed_at": previous.get("feedback_relayed_at") if previous else None,
    }
    save_leg(ctx, leg)
    return leg, True


def log_commits(ctx: Context, session: Optional[dict], leg: dict, session_id: Optional[str]) -> List[dict]:
    """Record commits the branch gained since the session last looked.

    Only the branch's own new commits count: commits merged in from the
    default branch are other people's, a rebase's copies of commits already
    logged aren't new, and after an amend or rebase only what follows the
    newest point the old and new histories share was made now.
    """
    repo = ctx.repo_root
    head = gitutil.head(repo)
    if not head or session is None:
        return []
    last = session.get("last_seen_head")
    if head == last:
        return []
    upstream = gitutil.upstream_refs(repo, ctx.branch)
    if last and gitutil.is_ancestor(repo, last, head):
        start: Optional[str] = last
    elif last or leg.get("base_ref"):
        candidates = [gitutil.merge_base(repo, last, head) if last and gitutil.rev_parse(repo, last) else None]
        candidates += [gitutil.merge_base(repo, ref, head) for ref in upstream]
        candidates = [c for c in candidates if c and c != head]
        start = gitutil.newest(repo, candidates) or (candidates[0] if candidates else leg.get("base_ref"))
        if last:
            session.setdefault("events", []).append(
                {"at": util.now_iso(), "kind": "history-rewritten", "from": last, "to": head})
    else:
        start = None  # the branch had no commits when the session started
    known_shas, known_pids = set(), set()
    for other in records.load_legs(ctx.feature_dir) + [leg]:
        for entry in other.get("commits", []):
            known_shas.add(entry.get("sha"))
            if entry.get("patch_id"):
                known_pids.add(entry["patch_id"])
    logged = []
    for sha in gitutil.branch_commits(repo, start, head, upstream):
        if sha in known_shas:
            continue
        meta = gitutil.commit_meta(repo, sha) or {}
        merge = len(meta.get("parents", [])) > 1
        pid = None if merge else gitutil.patch_id(repo, gitutil.commit_patch(repo, sha))
        if pid and pid in known_pids:
            continue  # a rebased copy of work already logged
        entry = {
            "sha": sha,
            "session_id": session_id,
            "logged_at": util.now_iso(),
            "subject": meta.get("subject", ""),
            "patch_id": pid,
        }
        if merge:
            entry["merge"] = True
        leg.setdefault("commits", []).append(entry)
        known_shas.add(sha)
        if pid:
            known_pids.add(pid)
        logged.append(entry)
    session["last_seen_head"] = head
    return logged


# --- sessions ----------------------------------------------------------------------


def new_session_id() -> str:
    return util.utcnow().strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(2)


def session_dir(ctx: Context, session_id: str) -> Path:
    return ctx.feature_dir / "sessions" / session_id


def load_session_meta(ctx: Context, session_id: str) -> Optional[dict]:
    data = util.read_json(session_dir(ctx, session_id) / "session.json", None)
    return data if isinstance(data, dict) else None


def save_session_meta(ctx: Context, meta: dict) -> None:
    util.write_json(session_dir(ctx, meta["session_id"]) / "session.json", meta)


_HARNESS_NAMES = {
    "claude": "claude-code",
    "codex": "codex",
    "pi": "pi",
    "devin": "devin-local",
    "windsurf": "devin-local",
    "cursor": "cursor",
    "aider": "aider",
    "gemini": "gemini-cli",
    "opencode": "opencode",
    "goose": "goose",
}


def _process_ancestry(limit: int = 12) -> List[List[str]]:
    """Command lines of this process's ancestors, nearest first."""
    chain: List[List[str]] = []
    pid = os.getppid()
    for _ in range(limit):
        if pid <= 1:
            break
        cmd: List[str] = []
        ppid = 0
        proc = Path(f"/proc/{pid}")
        if proc.exists():
            try:
                cmd = [p for p in (proc / "cmdline").read_bytes().decode("utf-8", "replace").split("\x00") if p]
                stat = (proc / "stat").read_text()
                ppid = int(stat.rsplit(")", 1)[1].split()[1])
            except (OSError, ValueError, IndexError):
                break
        else:
            try:
                out = subprocess.run(["ps", "-o", "ppid=,command=", "-p", str(pid)], stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, timeout=5).stdout.strip()
                ppid_text, _, command = out.partition(" ")
                ppid = int(ppid_text.strip() or 0)
                cmd = command.split()
            except (OSError, ValueError, subprocess.TimeoutExpired):
                break
        chain.append(cmd)
        pid = ppid
    return chain


def detect_harness() -> str:
    env = os.environ
    if env.get("DEBRIEF_HARNESS"):
        return env["DEBRIEF_HARNESS"]
    if env.get("CLAUDECODE") or env.get("CLAUDE_CODE_ENTRYPOINT"):
        return "claude-code"
    if env.get("PI_CODING_AGENT") or env.get("PI_CODING_AGENT_DIR"):
        return "pi"
    if any(key.startswith("CODEX_") for key in env):
        return "codex"
    if any(key.startswith(("DEVIN_", "WINDSURF_")) for key in env):
        return "devin-local"
    for cmd in _process_ancestry():
        for token in cmd[:3]:
            base = os.path.basename(token).lower()
            for name, harness in _HARNESS_NAMES.items():
                if base == name or base.startswith(name + "-") or base.startswith(name + "."):
                    return harness
    return "unknown"


def _journal_header(session_id: str) -> str:
    return (
        f"# Journal · session {session_id}\n\n"
        "<!-- Append entries below: a `### <time> · <kind>` heading, then 1-5 lines. -->\n\n"
    )


def _latest_session_with_journal(ctx: Context, exclude: Optional[str] = None) -> Optional[Path]:
    base = ctx.feature_dir / "sessions"
    if not base.is_dir():
        return None
    candidates = sorted((p for p in base.iterdir() if p.is_dir() and p.name != exclude), reverse=True)
    for path in candidates:
        journal = path / "journal.md"
        if journal.exists():
            return journal
    return None


def _pending_requests(ctx: Context, leg: Optional[dict], session: Optional[dict], mark: bool) -> List[str]:
    notes: List[str] = []
    closeout = skills_dir() / CLOSEOUT_SKILL
    if leg and leg.get("close_requested_at") and not leg.get("closed_at"):
        who = leg.get("close_requested_by") or "the developer"
        note = leg.get("close_note")
        notes.append(
            f"REQUEST from {who}: close out {leg['leg_id']} now. Follow {closeout} and complete every step."
            + (f" Note: {note}" if note else "")
        )
    feedback = ctx.feature_dir / "feedback.md"
    if feedback.exists() and leg is not None:
        items = parse_feedback(feedback.read_text(encoding="utf-8", errors="replace"))
        seen = leg.get("feedback_relayed_at") or ""
        fresh = [item for item in items if item["at"] > seen]
        if fresh:
            notes.append(
                f"FEEDBACK: {util.human_count(len(fresh), 'new review item')} in {feedback}. "
                "Read it, address each item, log what you change in your journal, and commit each fix atomically."
            )
            if mark:
                leg["feedback_relayed_at"] = max(item["at"] for item in fresh)
    if ctx.branch and leg and leg.get("branch") and leg.get("branch") != ctx.branch:
        notes.append(f"NOTE: HEAD is on branch {ctx.branch}, but this leg belongs to {leg.get('branch')}. Switch back "
                     f"to {leg.get('branch')}, or pass --feature <id> to keep this work in a feature of its own.")
    return notes


def parse_feedback(text: str) -> List[dict]:
    """feedback.md is a queue of `## <time> · <who>` sections written by the viewer."""
    items = []
    for line in text.splitlines():
        if line.startswith("## "):
            stamp = line[3:].split("·")[0].strip()
            if util.parse_iso(stamp):
                items.append({"at": stamp, "heading": line[3:].strip()})
    return items


def _maybe_pull(ctx: Context, force: bool = False) -> Optional[str]:
    """Pull the project's archive if it has a remote and the interval has passed."""
    if not archive.remote_url(ctx.project_dir):
        return None
    cfg = load_config()
    interval = cfg.get_int("sync", "pull_interval", 120)
    key = ctx.project_id
    with _state_lock():
        state = _load_state()
        last = util.parse_iso(state["pulls"].get(key))
        if not force and last and (util.utcnow() - last).total_seconds() < interval:
            return None
        state["pulls"][key] = util.now_iso()
        _save_state(state)
    try:
        with archive.lock(ctx.project_dir):
            archive.commit(ctx.project_dir, f"Checkpoint records from {util.hostname()}")
        result = archive.pull(ctx.project_dir, cfg, timeout=AGENT_NETWORK_TIMEOUT)
    except Exception as exc:  # network or residency problems must not stop the agent
        return f"archive pull failed: {exc}"
    if result.get("conflicts"):
        return f"archive pull kept both versions of: {', '.join(result['conflicts'])}"
    return None


# --- commands ------------------------------------------------------------------------


def cmd_start(ctx: Context, args) -> int:
    pull_note = _maybe_pull(ctx, force=True)
    ctx.feature_dir.mkdir(parents=True, exist_ok=True)
    with ctx.lock():
        leg, leg_created = ensure_open_leg(ctx)
        previous_id = current_session_id(ctx)
        previous = load_session_meta(ctx, previous_id) if previous_id else None
        if previous and previous.get("status") == "in_progress":
            # The previous run in this worktree ended without `close`. Commits made
            # since it last looked were made while it was open, so they are its.
            log_commits(ctx, previous, leg, previous_id)
            previous.setdefault("events", []).append({"at": util.now_iso(), "kind": "superseded"})
            save_session_meta(ctx, previous)
        session_id = new_session_id()
        meta = {
            "protocol": records.PROTOCOL,
            "session_id": session_id,
            "project_id": ctx.project_id,
            "feature_id": ctx.feature_id,
            "leg_id": leg["leg_id"],
            "harness": args.harness or detect_harness(),
            "model": args.model or None,
            "task": args.task or None,
            "host": util.hostname(),
            "branch": ctx.branch,
            "repo_root": str(ctx.repo_root),
            "started_at": util.now_iso(),
            "ended_at": None,
            "base_ref": ctx.head,
            "head_ref": None,
            "status": "in_progress",
            "last_seen_head": ctx.head,
        }
        sdir = session_dir(ctx, session_id)
        (sdir / "runs").mkdir(parents=True, exist_ok=True)
        util.write_text(sdir / "journal.md", _journal_header(session_id))
        save_session_meta(ctx, meta)
        leg.setdefault("sessions", []).append({
            "session_id": session_id,
            "harness": meta["harness"],
            "host": meta["host"],
            "started_at": meta["started_at"],
        })
        notes = _pending_requests(ctx, leg, meta, mark=True)
        save_leg(ctx, leg)
    _set_current(ctx, session_id)

    brief = ctx.feature_dir / "brief.md"
    prior_journal = _latest_session_with_journal(ctx, exclude=session_id)
    commits = len(leg.get("commits", []))
    out = [
        f"Debrief session {session_id} started.",
        f"  project  {ctx.project_id}",
        f"  feature  {ctx.feature_id}" + (f" (branch {ctx.branch})" if ctx.branch else " (detached HEAD)"),
        f"  leg      {leg['leg_id']}" + (" (new)" if leg_created else f" (open, {util.human_count(commits, 'commit')} so far)"),
        "Write records only in the feature directory, never in the repo:",
        f"  feature dir  {ctx.feature_dir}",
        f"  journal      {session_dir(ctx, session_id) / 'journal.md'}",
    ]
    if brief.exists():
        out.append(f"Existing brief: {brief}")
        if prior_journal:
            out.append(f"Read it and the latest journal ({prior_journal}) before you continue.")
        else:
            out.append("Read it before you continue.")
    elif prior_journal:
        out.append(f"Earlier session journal: {prior_journal} (read it before you continue).")
    if previous and previous.get("status") == "in_progress":
        out.append(f"The previous session here ({previous_id}) ended without `close`; its journal shows where it stopped.")
    if pull_note:
        out.append(f"Note: {pull_note}")
    out.extend(notes)
    out.append(f"Follow {skills_dir() / SESSION_SKILL}. First entry: ### {util.now_minute()} · plan")
    print("\n".join(out))
    return 0


def _resolve_session(ctx: Context) -> Tuple[Optional[str], Optional[dict]]:
    sid = current_session_id(ctx)
    return sid, (load_session_meta(ctx, sid) if sid else None)


def cmd_context(ctx: Context, args) -> int:
    sid, meta = _resolve_session(ctx)
    leg = open_leg(ctx)
    notes = _pending_requests(ctx, leg, meta, mark=False)
    if meta and meta.get("status") == "in_progress":
        print(
            f"Debrief session {sid} is open for feature {ctx.feature_id} ({meta.get('leg_id')}). "
            f"Keep appending to {session_dir(ctx, sid) / 'journal.md'}; get heading times from "
            f"`{launcher()} now`."
        )
    else:
        state = f"{leg['leg_id']} open" if leg else "no open leg"
        print(
            f"Debrief tracks this repo (feature {ctx.feature_id}, {state}). Before your first edit on a task "
            f"that changes behavior, run `{launcher()} start` and follow {skills_dir() / SESSION_SKILL}."
        )
    for note in notes:
        print(note)
    return 0


def cmd_now(ctx: Context, args) -> int:
    """Print the journal time stamp on stdout, and everything else on stderr.

    Agents often capture the stamp (``TS=$(debrief-session now)``); requests
    and feedback go to stderr so they are never swallowed by that capture.
    """
    stamp = util.now_minute()
    print(stamp, flush=True)
    if not ctx.feature_dir.exists():
        _say(f"No open Debrief session in this worktree; run `{launcher()} start` first.")
        return 0
    pull_note = _maybe_pull(ctx)
    sid, meta = _resolve_session(ctx)
    with ctx.lock():
        leg = open_leg(ctx)
        logged: List[dict] = []
        if leg and meta and meta.get("status") == "in_progress":
            logged = log_commits(ctx, meta, leg, sid)
            meta["last_activity_at"] = util.now_iso()
            save_session_meta(ctx, meta)
        notes = _pending_requests(ctx, leg, meta, mark=True)
        if leg:
            save_leg(ctx, leg)
    if not meta:
        _say(f"No open Debrief session in this worktree; run `{launcher()} start` first.")
    if logged:
        _say("Logged " + util.human_count(len(logged), "commit") + ": " + ", ".join(c["sha"][:7] for c in logged))
    if pull_note:
        _say(f"Note: {pull_note}")
    for note in notes:
        _say(note)
    return 0


def _say(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def launcher() -> str:
    """How agents should call this tool: the installed launcher's path, written the way install wrote it."""
    from . import install

    return install.session_launcher_text()


def cmd_run(ctx: Optional[Context], args) -> int:
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("usage: debrief-session run <command ...>", file=sys.stderr)
        return 2
    use_shell = len(command) == 1
    display = command[0] if use_shell else shlex.join(command)
    sid, meta = (_resolve_session(ctx) if ctx else (None, None))
    head = gitutil.head(ctx.repo_root) if ctx else None
    dirty = worktree_state(ctx.repo_root) if ctx and meta else {}
    started = util.utcnow()
    t0 = time.monotonic()
    tail: collections.deque = collections.deque(maxlen=OUTPUT_TAIL_LINES)
    code = 0
    try:
        proc = subprocess.Popen(
            command[0] if use_shell else command,
            shell=use_shell,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=os.getcwd(),
        )
    except OSError as exc:
        print(f"debrief-session run: {exc}", file=sys.stderr)
        tail.append(str(exc))
        code = 127
        proc = None
    if proc is not None:
        assert proc.stdout is not None
        sink = getattr(sys.stdout, "buffer", None)
        try:
            for raw in iter(proc.stdout.readline, b""):
                text = raw.decode("utf-8", "replace")
                if sink is not None:
                    sink.write(raw)
                    sink.flush()
                else:
                    sys.stdout.write(text)
                tail.append(text.rstrip("\n")[:2000])
            code = proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            code = 130
            tail.append("[interrupted]")
    duration = time.monotonic() - t0
    if ctx is None or meta is None:
        print("debrief-session: no open session here, so this run was not recorded.", file=sys.stderr)
        return code
    output = "\n".join(tail)
    if len(output) > OUTPUT_TAIL_CHARS:
        output = output[-OUTPUT_TAIL_CHARS:]
    record = {
        "command": display,
        "argv": command,
        "cwd": os.getcwd(),
        "repo_root": str(ctx.repo_root),
        "exit_code": code,
        "duration_s": round(duration, 3),
        "started_at": util.now_iso(started),
        "ended_at": util.now_iso(),
        "head": head,
        "dirty": bool(dirty),
        # What the run saw on top of HEAD, so ingest can tell whether the tested code is what got committed.
        "dirty_files": dirty if len(dirty) <= MAX_DIRTY_FILES else None,
        "session_id": sid,
        "output_tail": output,
    }
    name = started.strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(2) + ".json"
    util.write_json(session_dir(ctx, sid) / "runs" / name, record)
    with ctx.lock():
        leg = open_leg(ctx)
        if leg:
            log_commits(ctx, meta, leg, sid)
            save_leg(ctx, leg)
        meta["last_activity_at"] = util.now_iso()
        save_session_meta(ctx, meta)
    status = "passed" if code == 0 else f"failed (exit {code})"
    print(f"[debrief] recorded run: {display} {status} in {duration:.1f}s", file=sys.stderr)
    return code


def worktree_state(repo: Path) -> Dict[str, Optional[str]]:
    """Uncommitted files and their blob ids (None for deletions), hashed without writing to the repo."""
    paths = [path for code, path in gitutil.status_porcelain(repo)]
    if len(paths) > MAX_DIRTY_FILES:
        return {p: None for p in paths}
    return gitutil.hash_files(repo, paths)


def feature_base(ctx: Context) -> Optional[str]:
    legs = records.load_legs(ctx.feature_dir)
    if legs and legs[0].get("base_ref"):
        return legs[0]["base_ref"]
    return _initial_base(ctx)


def _evidence(ctx: Context) -> Tuple[Optional[dict], Optional[str]]:
    """Compute the feature's evidence now (it lives in the archive). Returns (evidence, problem)."""
    from . import ingest

    try:
        return ingest.ingest_feature(ctx.project_id, ctx.feature_id, repo=ctx.repo_root), None
    except Exception as exc:  # evidence must never block the agent
        return None, f"evidence not computed: {exc}"


def _ranges(lines: List[int]) -> str:
    return ", ".join(str(n) for n in lines[:6]) + (" ..." if len(lines) > 6 else "")


def explain_file(f: dict) -> str:
    """What explains one changed file, hunk by hunk, as `bin/session changed` prints it."""
    hunks = f.get("hunks") or []
    strong = sorted({c["by"] for h in hunks for c in h.get("claims", []) if c.get("strength") == "strong"})
    open_lines: List[int] = []
    weak = set()
    for h in hunks:
        if h["state"] == "unclaimed" and not h.get("whitespace_only"):
            blocks = [b for b in h.get("blocks") or [] if b["state"] == "unclaimed"]
            if blocks:
                open_lines += [(b["new"] or b["old"] or [h.get("new_start")])[0] for b in blocks]
            else:
                open_lines.append(h.get("new_start") or h.get("old_start") or 1)
        elif h["state"] == "weak":
            weak.update(c["by"] for c in h.get("claims", []))
    if open_lines:
        where = "NOT EXPLAINED" + (f" at line {_ranges(open_lines)}" if f.get("status") != "D" else "")
        return where + (f"; the rest by {', '.join(strong)}" if strong else "")
    if weak:
        return "path anchor only (weak): " + ", ".join(sorted(weak))
    if strong:
        return "explained by " + ", ".join(strong)
    if f.get("noise"):
        return f"incidental ({f['noise']})"
    return "incidental"


def unexplained(evidence: Optional[dict]) -> List[str]:
    """``path`` or ``path:lines`` for every changed file with a hunk nothing explains."""
    out = []
    for f in (evidence or {}).get("files") or []:
        why = explain_file(f)
        if not why.startswith("NOT EXPLAINED"):
            continue
        lines = why.split(" at line ", 1)[1].split(";")[0] if " at line " in why else ""
        out.append(f"{f['path']}:{lines}" if lines else f["path"])
    return out


def changed_files(ctx: Context) -> Tuple[Optional[str], List[Tuple[str, str]], List[Tuple[str, str]]]:
    """(feature base, committed changes since base, uncommitted changes)."""
    base = feature_base(ctx)
    committed: List[Tuple[str, str]] = []
    if ctx.head:
        committed = gitutil.name_status(ctx.repo_root, base or gitutil.empty_tree(ctx.repo_root), ctx.head)
    uncommitted = [(code.strip() or "?", path) for code, path in gitutil.status_porcelain(ctx.repo_root)]
    return base, committed, uncommitted


def cmd_changed(ctx: Context, args) -> int:
    """List what the feature changed and what explains each file, from freshly computed evidence."""
    base, committed, uncommitted = changed_files(ctx)
    leg = open_leg(ctx)
    leg_files = set()
    if leg and ctx.head:
        leg_base = leg.get("base_ref") or gitutil.empty_tree(ctx.repo_root)
        leg_files = {p for _, p in gitutil.name_status(ctx.repo_root, leg_base, ctx.head)}
    print(f"Feature {ctx.feature_id}: {base[:9] if base else '(start)'}..{(ctx.head or '?')[:9]}"
          + (f", {leg['leg_id']} open" if leg else ", no open leg"))
    evidence, problem = _evidence(ctx) if ctx.feature_dir.exists() else (None, None)
    files = (evidence or {}).get("files") or []
    if problem:
        print(f"({problem})")
    elif evidence and evidence.get("stale_note"):
        print(f"({evidence['stale_note']})")
    if files:
        width = max(len(f["path"]) for f in files)
        print("Changes (* = changed in the open leg), judged hunk by hunk:")
        for f in files:
            mark = "*" if f["path"] in leg_files else " "
            print(f" {mark}{(f.get('status') or 'M')[:1]} {f['path'].ljust(width)}  {explain_file(f)}")
        missing = unexplained(evidence)
        if missing:
            print(f"{util.human_count(len(missing), 'file')} with unexplained changes: anchor each changed line from a "
                  "system (symbol or line range), or list the file under `incidental` in brief.md.")
        print(ingest_summary(evidence))
    elif committed:
        for code, path in committed:
            print(f"  {code[:1]} {path}")
    else:
        print("No committed changes since the feature base.")
    if uncommitted:
        print("Uncommitted (commit these atomically before closeout):")
        for code, path in uncommitted:
            print(f"  {code:>2} {path}")
    return 0


def ingest_summary(evidence: dict) -> str:
    from . import ingest

    return ingest.summary_line(evidence)


def _close_session(ctx: Context, sid: str, meta: dict, status: str) -> List[dict]:
    leg = open_leg(ctx)
    logged = log_commits(ctx, meta, leg, sid) if leg else []
    head = gitutil.head(ctx.repo_root)
    dirty = bool([c for c, _ in gitutil.status_porcelain(ctx.repo_root)])
    meta["status"] = status
    meta["ended_at"] = util.now_iso()
    meta["head_ref"] = "WORKTREE" if dirty else head
    meta["head_commit"] = head
    save_session_meta(ctx, meta)
    if leg:
        save_leg(ctx, leg)
    return logged


def cmd_close(ctx: Context, args) -> int:
    status = args.status
    sid, meta = _resolve_session(ctx)
    if not meta:
        print("No open Debrief session in this worktree.")
        return 1
    with ctx.lock():
        logged = _close_session(ctx, sid, meta, status)
    _set_current(ctx, None)
    journal = (session_dir(ctx, sid) / "journal.md").read_text(encoding="utf-8", errors="replace")
    entries, _ = records.parse_journal(journal)
    print(f"Session {sid} closed ({status}).")
    if logged:
        print("Logged " + util.human_count(len(logged), "commit") + ".")
    if not any(e["kind"] == "handoff" for e in entries):
        print(f"Warning: the journal has no `handoff` entry. Append one now: ### {util.now_minute()} · handoff")
    if meta.get("head_ref") == "WORKTREE":
        print("Warning: uncommitted changes remain in the worktree.")
    return 0


def cmd_publish(ctx: Context, args) -> int:
    leg = open_leg(ctx)
    if not leg:
        print(f"No open leg for feature {ctx.feature_id}; nothing to publish.")
        return 1
    status = gitutil.status_porcelain(ctx.repo_root)
    tracked_dirty = [p for c, p in status if c != "??"]
    untracked = [p for c, p in status if c == "??"]
    if (tracked_dirty or untracked) and not args.force:
        print("Refusing to publish: commit the remaining code first (closeout step 1):")
        for path in tracked_dirty[:20]:
            print(f"  {path}")
        for path in untracked[:20]:
            print(f"  {path} (untracked: commit it if it belongs to the change, or delete it)")
        print("Pass --force to publish with uncommitted changes.")
        return 1
    feature = records.load_feature(ctx.feature_dir)
    _base, committed, _uncommitted = changed_files(ctx)
    errors = records.closeout_issues(feature, code_changed=bool(committed))
    warnings = records.all_issues(feature)
    errors += [w for w in warnings if w["level"] == "error"]
    warnings = [w for w in warnings if w["level"] != "error"]
    if errors and not args.force:
        print("Refusing to publish; fix these first (or pass --force):")
        for item in errors:
            print(f"  {item['record']}: {item['message']}")
        return 1
    sid, meta = _resolve_session(ctx)
    with ctx.lock():
        leg = open_leg(ctx) or leg
        if meta and meta.get("status") == "in_progress":
            _close_session(ctx, sid, meta, "complete")
            leg = open_leg(ctx) or leg
        head = gitutil.head(ctx.repo_root)
        leg["head_commit"] = head
        leg["head_ref"] = "WORKTREE" if tracked_dirty else head
        leg["head_tree"] = gitutil.tree_of(ctx.repo_root, head) if head else None
        leg["closed_at"] = util.now_iso()
        leg["closed_by"] = (meta or {}).get("harness") or "cli"
        save_leg(ctx, leg)
    if sid:
        _set_current(ctx, None)
    evidence, evidence_note = _evidence(ctx)
    if evidence is not None:
        evidence_note = evidence.get("stale_note") or ingest_summary(evidence)
    title = ((feature.get("brief") or {}).get("meta") or {}).get("title") or ctx.feature_id
    message = f"Close {leg['leg_id']} of {ctx.feature_id}: {title}"
    sync = archive.sync(ctx.project_dir, message, timeout=PUBLISH_NETWORK_TIMEOUT)
    print(f"Published {leg['leg_id']} of {ctx.feature_id}.")
    if sync.get("committed"):
        print(f"  archive commit {sync['committed'][:9]}")
    if sync.get("pushed"):
        print("  pushed to the archive remote")
    elif archive.remote_url(ctx.project_dir):
        print(f"  not pushed: {sync.get('reason', 'unknown error')}")
    if evidence_note:
        print(f"  {evidence_note}")
    if warnings:
        print(f"{util.human_count(len(warnings), 'record warning')} (run `debrief check` for all):")
        for item in warnings[:12]:
            print(f"  {item['record']}: {item['message']}")
    missing = unexplained(evidence)
    if missing:
        print("Changes nothing explains: " + ", ".join(missing[:15]))
    return 0


# --- entry point ------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="debrief-session", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--feature", help="feature id to use instead of the branch name")
    sub = parser.add_subparsers(dest="cmd", metavar="command")
    start = sub.add_parser("start", help="open a session for the current branch")
    start.add_argument("--task", help="one-line restatement of the assignment")
    start.add_argument("--harness", help="harness name if detection gets it wrong")
    start.add_argument("--model", help="model name, if known")
    sub.add_parser("context", help="print session state without changing it (for hooks)")
    sub.add_parser("status", help="same as context")
    sub.add_parser("now", help="print the journal time stamp and relay requests")
    run = sub.add_parser("run", help="run a command and record the result")
    run.add_argument("command", nargs=argparse.REMAINDER)
    sub.add_parser("changed", help="list changed files and what explains them")
    close = sub.add_parser("close", help="end this session")
    close.add_argument("status", nargs="?", default="complete", choices=["complete", "blocked", "abandoned"])
    publish = sub.add_parser("publish", help="close out the leg and sync its records")
    publish.add_argument("--force", action="store_true", help="publish despite missing records or uncommitted code")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    try:
        return _main(argv)
    except BrokenPipeError:
        # The reader stopped early (`| head`); state is already saved, so just stop quietly.
        quiet_stdout()
        return 0


def quiet_stdout() -> None:
    """Point stdout at /dev/null so the interpreter's final flush can't fail on a closed pipe."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    except (OSError, ValueError, AttributeError):
        pass


def _main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 0
    try:
        ctx: Optional[Context] = Context(Path.cwd(), args.feature)
    except Untracked as exc:
        if args.cmd == "run":
            return cmd_run(None, args)
        if args.cmd == "now":
            print(util.now_minute())
        print(str(exc))
        return 0
    handlers = {
        "start": cmd_start,
        "context": cmd_context,
        "status": cmd_context,
        "now": cmd_now,
        "run": cmd_run,
        "changed": cmd_changed,
        "close": cmd_close,
        "publish": cmd_publish,
    }
    return handlers[args.cmd](ctx, args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

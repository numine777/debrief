"""``debrief install``: put the protocol where each agent harness looks for it.

Per host, never per project (goal G7). For every harness found it writes:

* the always-on instruction block, between marker comments, so upgrades replace
  only that block and leave the rest of the file alone;
* the two skills, once, in ``~/.agents/skills`` (Devin, Codex and pi read it;
  Claude Code gets symlinks from ``~/.claude/skills``);
* ``debrief`` and ``debrief-session`` launchers in ``~/.local/bin`` that run the
  zipapp kept in ``~/.local/share/debrief``.

Devin also reads ``~/.claude/CLAUDE.md``, so when Claude Code is present too the
block is written there once instead of twice (unless ``CLAUDE_CONFIG_DIR`` moves
Claude Code's file somewhere Devin does not look).
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Dict, List, Optional

from . import __version__, buildzip, paths, resources, util

BUNDLE_VERSION = int(resources.read_text("bundle/skills/ai-session/VERSION").strip() or "1")
BEGIN_RE = re.compile(r"<!-- debrief:begin v(\d+) -->")
END_MARK = "<!-- debrief:end -->"
MANIFEST = ".debrief-manifest"
SKILLS = ("ai-session", "ai-session-closeout")
HARNESSES = ("devin", "claude", "codex", "pi")
CODEX_LIMIT = 32 * 1024


def home() -> Path:
    return Path.home()


def shared_skills_dir() -> Path:
    return home() / ".agents" / "skills"


def claude_dir() -> Path:
    value = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(value).expanduser() if value else home() / ".claude"


def codex_dir() -> Path:
    value = os.environ.get("CODEX_HOME")
    return Path(value).expanduser() if value else home() / ".codex"


def pi_dir() -> Path:
    value = os.environ.get("PI_CODING_AGENT_DIR")
    return Path(value).expanduser() if value else home() / ".pi" / "agent"


def devin_dir() -> Path:
    return home() / ".config" / "devin"


def block_file(harness: str) -> Path:
    return {
        "devin": devin_dir() / "AGENTS.md",
        "claude": claude_dir() / "CLAUDE.md",
        "codex": codex_dir() / "AGENTS.md",
        "pi": pi_dir() / "AGENTS.md",
    }[harness]


def devin_reads_claude(chosen: List[str]) -> bool:
    """Whether Devin gets the block from Claude Code's file instead of its own.

    Devin reads ``~/.claude/CLAUDE.md``; a ``CLAUDE_CONFIG_DIR`` elsewhere means it
    needs its own copy.
    """
    return ("devin" in chosen and "claude" in chosen
            and block_file("claude") == home() / ".claude" / "CLAUDE.md")


def detect() -> Dict[str, str]:
    """Harnesses that appear to be installed on this host, with the reason."""
    found: Dict[str, str] = {}
    which = shutil.which
    if devin_dir().exists():
        found["devin"] = f"found {devin_dir()}"
    else:
        for name in (".devin", ".devin-server", ".windsurf-server"):
            if (home() / name).exists():
                found["devin"] = f"found ~/{name}"
                break
        else:
            if which("devin"):
                found["devin"] = "devin is on PATH"
    if os.environ.get("CLAUDE_CONFIG_DIR") or claude_dir().exists():
        found["claude"] = f"found {claude_dir()}"
    elif which("claude"):
        found["claude"] = "claude is on PATH"
    if os.environ.get("CODEX_HOME") or codex_dir().exists():
        found["codex"] = f"found {codex_dir()}"
    elif which("codex"):
        found["codex"] = "codex is on PATH"
    if os.environ.get("PI_CODING_AGENT_DIR") or pi_dir().exists():
        found["pi"] = f"found {pi_dir()}"
    elif which("pi"):
        found["pi"] = "pi is on PATH"
    return found


# --- the always-on block --------------------------------------------------------


def session_launcher_text() -> str:
    """The launcher path as agents should type it (``~/...`` when under home)."""
    return _tilde(paths.user_bin_dir() / "debrief-session")


def render_text(text: str, skills: Optional[Path] = None) -> str:
    """Fill the bundle's placeholders with this host's paths."""
    return text.replace("<skills>", _tilde(skills or shared_skills_dir())).replace("<session>", session_launcher_text())


def render_block(skills: Optional[Path] = None) -> str:
    body = render_text(resources.read_text("bundle/block.md"), skills).rstrip("\n")
    return f"<!-- debrief:begin v{BUNDLE_VERSION} -->\n{body}\n{END_MARK}\n"


def _tilde(path: Path) -> str:
    try:
        return "~/" + str(Path(path).relative_to(home()))
    except ValueError:
        return str(path)


def nix_managed(path: Path) -> bool:
    """Whether ``path`` is a link into the Nix store, as home-manager installs files.

    Such files belong to the user's Nix configuration: writing through or over the
    link would either fail (the store is read-only) or break the next activation.
    """
    store = (os.environ.get("NIX_STORE_DIR") or "/nix/store").rstrip("/") + "/"
    path = Path(path)
    try:
        return path.is_symlink() and os.path.realpath(path).startswith(store)
    except OSError:
        return False


def _managed_note(path: Path) -> str:
    return f"left {_tilde(path)} alone: Nix manages it"


def _find_block(text: str):
    begin = BEGIN_RE.search(text)
    if not begin:
        return None
    end = text.find(END_MARK, begin.end())
    if end < 0:
        return None
    stop = end + len(END_MARK)
    if text[stop:stop + 1] == "\n":
        stop += 1
    return begin.start(), stop, int(begin.group(1))


def upsert_block(path: Path, block: str, dry_run: bool = False) -> str:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    found = _find_block(text)
    if found:
        start, stop, _version = found
        if text[start:stop] == block:
            return "unchanged"
        new = text[:start] + block + text[stop:]
        action = "updated"
    else:
        if not text or text.endswith("\n\n"):
            new = text + block
        elif text.endswith("\n"):
            new = text + "\n" + block
        else:
            new = text + "\n\n" + block
        action = "added"
    if not dry_run:
        util.write_text(path, new)
    return action


def remove_block(path: Path, dry_run: bool = False) -> bool:
    if not path.exists() or nix_managed(path):
        return False
    text = path.read_text(encoding="utf-8")
    found = _find_block(text)
    if not found:
        return False
    start, stop, _ = found
    before = text[:start].rstrip("\n")
    after = text[stop:].lstrip("\n")
    if before and after:
        new = before + "\n\n" + after
    elif before:
        new = before + "\n"
    else:
        new = after
    if not dry_run:
        util.write_text(path, new)
    return True


# --- skills ------------------------------------------------------------------------


def _bundle_skill_files(skill: str) -> List[str]:
    prefix = f"bundle/skills/{skill}"
    return [rel[len(prefix) + 1:] for rel in resources.list_files(prefix)]


def install_skill(skill: str, dest_root: Path, dry_run: bool = False) -> str:
    dest = dest_root / skill
    files = _bundle_skill_files(skill)
    if nix_managed(dest):
        return _managed_note(dest)
    if dest.is_symlink() or (dest.exists() and not dest.is_dir()):
        return f"skipped {_tilde(dest)}: not a directory Debrief manages"
    if dest.exists() and not (dest / MANIFEST).exists() and any(dest.iterdir()):
        return f"skipped {_tilde(dest)}: exists and was not installed by Debrief"
    old = set()
    if (dest / MANIFEST).exists():
        old = set((dest / MANIFEST).read_text(encoding="utf-8").split())
    changed = False
    for rel in files:
        data = resources.read_bytes(f"bundle/skills/{skill}/{rel}")
        if rel.endswith(".md"):
            data = render_text(data.decode("utf-8"), dest_root).encode("utf-8")
        target = dest / rel
        if not target.exists() or target.read_bytes() != data:
            changed = True
            if not dry_run:
                util.write_bytes(target, data)
    for rel in sorted(old - set(files)):
        stale = dest / rel
        if stale.exists():
            changed = True
            if not dry_run:
                stale.unlink()
    if not dry_run:
        util.write_text(dest / MANIFEST, "\n".join(files) + "\n")
    return ("installed" if changed else "unchanged") + f" {_tilde(dest)}"


def remove_skill(skill: str, dest_root: Path, dry_run: bool = False) -> Optional[str]:
    dest = dest_root / skill
    if nix_managed(dest):
        return None
    if dest.is_symlink():
        if not dry_run:
            dest.unlink()
        return f"removed link {_tilde(dest)}"
    if (dest / MANIFEST).exists():
        if not dry_run:
            shutil.rmtree(dest)
        return f"removed {_tilde(dest)}"
    return None


def link_skill(skill: str, link_root: Path, target_root: Path, dry_run: bool = False) -> str:
    link = link_root / skill
    target = target_root / skill
    if nix_managed(link):
        return _managed_note(link)
    if link.is_symlink():
        if Path(os.readlink(link)) == target:
            return f"unchanged link {_tilde(link)}"
        if not dry_run:
            link.unlink()
    elif link.exists():
        if (link / MANIFEST).exists():
            if not dry_run:
                shutil.rmtree(link)
        else:
            return f"skipped {_tilde(link)}: exists and was not installed by Debrief"
    if not dry_run:
        link_root.mkdir(parents=True, exist_ok=True)
        os.symlink(target, link)
    return f"linked {_tilde(link)} -> {_tilde(target)}"


# --- launchers -----------------------------------------------------------------------


def launcher_text(name: str, subcommand: str, pyz: Path) -> str:
    text = resources.read_text("bundle/shim.sh")
    return (text.replace("@NAME@", name)
                .replace("@VERSION@", __version__)
                .replace("@PYZ@", str(pyz))
                .replace("@SUBCOMMAND@", f"{subcommand} " if subcommand else ""))


def install_tool(dry_run: bool = False) -> List[str]:
    pyz = paths.tool_home() / "debrief.pyz"
    bindir = paths.user_bin_dir()
    names = (("debrief", ""), ("debrief-session", "session"))
    if any(nix_managed(bindir / name) for name, _ in names):
        # A Nix package (the home-manager module) provides the launchers and the zipapp.
        return [_managed_note(bindir / name) for name, _ in names if nix_managed(bindir / name)]
    actions = []
    if not dry_run:
        buildzip.install_copy(pyz)
    actions.append(f"installed {_tilde(pyz)}")
    for name, sub in names:
        target = bindir / name
        text = launcher_text(name, sub, pyz)
        if target.exists() and target.read_text(encoding="utf-8", errors="replace") == text:
            actions.append(f"unchanged {_tilde(target)}")
            continue
        if not dry_run:
            util.write_text(target, text)
            os.chmod(target, 0o755)
        actions.append(f"installed {_tilde(target)}")
    if str(bindir) not in os.environ.get("PATH", "").split(os.pathsep):
        actions.append(f"note: {_tilde(bindir)} is not on PATH; agents call it by full path, which works")
    return actions


# --- Claude Code settings ----------------------------------------------------------------


def claude_settings(dry_run: bool = False, remove: bool = False) -> List[str]:
    """SessionStart hook, archive access and allow rules in ~/.claude/settings.json."""
    path = claude_dir() / "settings.json"
    if nix_managed(path):
        return [_managed_note(path) + "; add Debrief's hook and allow rules to that configuration"]
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except ValueError:
        return [f"skipped {path}: not valid JSON"]
    if not isinstance(data, dict):
        return [f"skipped {path}: not a JSON object"]
    before = json.dumps(data, sort_keys=True)
    session_cmd = str(paths.user_bin_dir() / "debrief-session")
    hooks = data.setdefault("hooks", {})
    starts = [h for h in hooks.get("SessionStart", []) if not _is_debrief_hook(h)]
    if not remove:
        starts.append({"hooks": [{"type": "command", "command": f"{session_cmd} context"}]})
    if starts:
        hooks["SessionStart"] = starts
    else:
        hooks.pop("SessionStart", None)
    if not hooks:
        data.pop("hooks", None)
    perms = data.setdefault("permissions", {})
    archive_dir = str(paths.archive_root())
    dirs = [d for d in perms.get("additionalDirectories", []) if d != archive_dir]
    allow = [r for r in perms.get("allow", []) if "debrief-session" not in r]
    if not remove:
        dirs.append(archive_dir)
        # Rules match the command as typed: the absolute path, the ~ form the instructions use, or the bare name.
        forms = list(dict.fromkeys([session_cmd, session_launcher_text(), "debrief-session"]))
        for sub in ("start", "context", "now", "changed", "close", "publish"):
            for form in forms:
                allow.append(f"Bash({form} {sub}:*)")
    for key, value in (("additionalDirectories", dirs), ("allow", allow)):
        if value:
            perms[key] = value
        else:
            perms.pop(key, None)
    if not perms:
        data.pop("permissions", None)
    if json.dumps(data, sort_keys=True) == before:
        # Compared as data, so a file Claude Code formatted its own way is not rewritten.
        return [f"unchanged {_tilde(path)}"]
    if not dry_run:
        util.write_text(path, json.dumps(data, indent=2) + "\n")
    verb = "removed Debrief entries from" if remove else "updated"
    return [f"{verb} {_tilde(path)} (SessionStart hook, archive directory, allow rules)"]


def _is_debrief_hook(entry: dict) -> bool:
    return any("debrief-session" in str(h.get("command", "")) for h in entry.get("hooks", []) if isinstance(h, dict))


# --- install / uninstall ------------------------------------------------------------------


def run_install(harnesses: Optional[List[str]] = None, claude_hooks: bool = False,
                dry_run: bool = False) -> List[str]:
    report: List[str] = []
    detected = detect()
    if harnesses is None:
        chosen = [h for h in HARNESSES if h in detected]
        for name in HARNESSES:
            report.append(f"{name}: " + (f"detected ({detected[name]})" if name in detected else "not found"))
    else:
        chosen = [h for h in harnesses if h != "none"]
    report.extend(install_tool(dry_run))
    if not chosen:
        report.append("No harness selected: installed the launchers only. Use --harness to choose one.")
        return report
    shared = shared_skills_dir()
    for skill in SKILLS:
        report.append(install_skill(skill, shared, dry_run))
    block = render_block(shared)
    devin_via_claude = devin_reads_claude(chosen)
    for harness in chosen:
        if harness == "devin" and devin_via_claude:
            if remove_block(block_file("devin"), dry_run):
                report.append(f"removed the duplicate block from {_tilde(block_file('devin'))}")
            report.append(f"devin: reads the block from {_tilde(block_file('claude'))}")
            continue
        target = block_file(harness)
        if nix_managed(target):
            report.append(f"{harness}: {_managed_note(target)}; include the block there yourself")
            continue
        action = upsert_block(target, block, dry_run)
        report.append(f"{harness}: {action} block in {_tilde(target)}")
        if harness == "codex" and target.exists() and target.stat().st_size > CODEX_LIMIT:
            report.append(f"warning: {_tilde(target)} exceeds Codex's 32 KiB instruction limit")
        if harness == "claude":
            for skill in SKILLS:
                report.append(link_skill(skill, claude_dir() / "skills", shared, dry_run))
    if claude_hooks and "claude" in chosen:
        report.extend(claude_settings(dry_run))
    report.append(f"Records go to {paths.archive_root()}. Track a repo with `debrief init <repo>`.")
    return report


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def run_managed(harnesses: List[str], claude_hooks: bool = False, dry_run: bool = False,
                quiet: bool = False) -> List[str]:
    """Install for a package that provides the launchers and skills itself.

    The home-manager module installs ``debrief-session`` and the skills as Nix-managed
    files and calls this on every activation. It keeps the instruction block in the
    chosen harnesses' files and removes it from the others, and adds or removes
    Claude Code's settings, so those files follow the configuration. Files that Nix
    manages are never written; the report says when one lacks the current block.
    With ``quiet``, only changes and problems are reported.
    """
    chosen = [h for h in harnesses if h != "none"]
    report: List[str] = []
    block = render_block(shared_skills_dir())
    devin_via_claude = devin_reads_claude(chosen)
    for harness in HARNESSES:
        target = block_file(harness)
        wanted = harness in chosen and not (harness == "devin" and devin_via_claude)
        if nix_managed(target):
            if wanted:
                text = _read(target)
                found = _find_block(text)
                if found and text[found[0]:found[1]] == block:
                    if not quiet:
                        report.append(f"{harness}: {_tilde(target)} comes from Nix and has the block")
                else:
                    state = "an outdated block" if found else "no block"
                    report.append(f"{harness}: {_tilde(target)} comes from Nix and has {state}; "
                                  "include programs.debrief.blockText in it")
            continue
        if wanted:
            action = upsert_block(target, block, dry_run)
            if action != "unchanged" or not quiet:
                report.append(f"{harness}: {action} block in {_tilde(target)}")
            if harness == "codex" and target.exists() and target.stat().st_size > CODEX_LIMIT:
                report.append(f"warning: {_tilde(target)} exceeds Codex's 32 KiB instruction limit")
        elif remove_block(target, dry_run):
            report.append(f"{harness}: removed block from {_tilde(target)}")
    if devin_via_claude and not quiet:
        report.append(f"devin: reads the block from {_tilde(block_file('claude'))}")
    settings = claude_dir() / "settings.json"
    if claude_hooks and "claude" in chosen:
        if not nix_managed(settings):
            report.extend(line for line in claude_settings(dry_run) if not (quiet and line.startswith("unchanged")))
        elif "debrief-session" not in _read(settings):
            report.append(f"claude: {_tilde(settings)} comes from Nix; merge programs.debrief.claudeCodeSettings "
                          "into programs.claude-code.settings")
        elif not quiet:
            report.append(f"claude: {_tilde(settings)} comes from Nix and has Debrief's entries")
    elif not nix_managed(settings) and "debrief-session" in _read(settings):
        report.extend(claude_settings(dry_run, remove=True))
    return report


def render_agent_files(dest: Path) -> List[str]:
    """Write the instruction block and skills, rendered for this host's paths, into ``dest``.

    For packages that install them themselves: ``dest/block.md`` and
    ``dest/skills/<skill>/``. Nothing else is touched.
    """
    dest = Path(dest)
    shared = shared_skills_dir()
    util.write_text(dest / "block.md", render_block(shared))
    for skill in SKILLS:
        for rel in _bundle_skill_files(skill):
            data = resources.read_bytes(f"bundle/skills/{skill}/{rel}")
            if rel.endswith(".md"):
                data = render_text(data.decode("utf-8"), shared).encode("utf-8")
            util.write_bytes(dest / "skills" / skill / rel, data)
    return [f"rendered the block and skills into {dest} for {session_launcher_text()} and {_tilde(shared)}"]


def run_uninstall(dry_run: bool = False) -> List[str]:
    report: List[str] = []
    for harness in HARNESSES:
        if remove_block(block_file(harness), dry_run):
            report.append(f"removed block from {_tilde(block_file(harness))}")
    for skill in SKILLS:
        for root in (claude_dir() / "skills", shared_skills_dir()):
            result = remove_skill(skill, root, dry_run)
            if result:
                report.append(result)
    settings = claude_dir() / "settings.json"
    if settings.exists() and "debrief-session" in settings.read_text(encoding="utf-8", errors="replace"):
        report.extend(claude_settings(dry_run, remove=True))
    for name in ("debrief", "debrief-session"):
        target = paths.user_bin_dir() / name
        if target.exists() and "installed by `debrief install`" in target.read_text(encoding="utf-8", errors="replace"):
            if not dry_run:
                target.unlink()
            report.append(f"removed {_tilde(target)}")
    pyz = paths.tool_home() / "debrief.pyz"
    if pyz.exists():
        if not dry_run:
            pyz.unlink()
        report.append(f"removed {_tilde(pyz)}")
    report.append(f"Kept the archive at {paths.archive_root()}.")
    return report

"""Git access.

Project repositories are only ever read: every helper here runs with
``GIT_OPTIONAL_LOCKS=0`` so even ``git status`` won't rewrite the index, and
diffs disable external diff drivers and textconv filters. Writes happen only
in Debrief's own archive repositories, through :mod:`debrief.archive`.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_PAGER": "cat",
    "PAGER": "cat",
    "LC_ALL": "C",
    "GIT_CONFIG_NOSYSTEM": os.environ.get("GIT_CONFIG_NOSYSTEM", ""),
}

BASE_ARGS = ["-c", "core.quotepath=off", "-c", "color.ui=false"]


class GitError(RuntimeError):
    def __init__(self, args: Sequence[str], code: int, stderr: str):
        self.args_list = list(args)
        self.code = code
        self.stderr = stderr.strip()
        super().__init__(f"git {' '.join(args)} failed ({code}): {self.stderr}")


def _env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = dict(os.environ)
    env.update({k: v for k, v in GIT_ENV.items() if v != ""})
    if extra:
        env.update(extra)
    return env


def run_bytes(
    args: Sequence[str],
    cwd: Path | str,
    check: bool = True,
    input_bytes: Optional[bytes] = None,
    env_extra: Optional[Dict[str, str]] = None,
    ok_codes: Tuple[int, ...] = (0,),
    timeout: Optional[float] = None,
) -> bytes:
    try:
        proc = subprocess.run(
            ["git", *BASE_ARGS, *args],
            cwd=str(cwd),
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_env(env_extra),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise GitError(args, -1, f"timed out after {timeout:.0f}s") from None
    if check and proc.returncode not in ok_codes:
        raise GitError(args, proc.returncode, proc.stderr.decode("utf-8", "replace"))
    return proc.stdout


def run(
    args: Sequence[str],
    cwd: Path | str,
    check: bool = True,
    input_text: Optional[str] = None,
    env_extra: Optional[Dict[str, str]] = None,
    ok_codes: Tuple[int, ...] = (0,),
    timeout: Optional[float] = None,
) -> str:
    data = run_bytes(
        args,
        cwd,
        check=check,
        input_bytes=input_text.encode("utf-8") if input_text is not None else None,
        env_extra=env_extra,
        ok_codes=ok_codes,
        timeout=timeout,
    )
    return data.decode("utf-8", "replace")


def try_run(args: Sequence[str], cwd: Path | str) -> Optional[str]:
    try:
        return run(args, cwd).strip()
    except (GitError, FileNotFoundError, NotADirectoryError):
        return None


# --- repository identity -------------------------------------------------


def toplevel(path: Path | str) -> Optional[Path]:
    out = try_run(["rev-parse", "--show-toplevel"], path)
    return Path(out) if out else None


def common_dir(path: Path | str) -> Optional[Path]:
    """The shared git directory: the same for every worktree of a repo."""
    out = try_run(["rev-parse", "--path-format=absolute", "--git-common-dir"], path)
    if out:
        return Path(out).resolve()
    out = try_run(["rev-parse", "--git-common-dir"], path)
    if not out:
        return None
    candidate = Path(out)
    if not candidate.is_absolute():
        candidate = (Path(path) / candidate)
    return candidate.resolve()


def current_branch(path: Path | str) -> Optional[str]:
    return try_run(["symbolic-ref", "--short", "-q", "HEAD"], path) or None


def head(path: Path | str) -> Optional[str]:
    return try_run(["rev-parse", "--verify", "-q", "HEAD"], path) or None


def rev_parse(path: Path | str, rev: str) -> Optional[str]:
    return try_run(["rev-parse", "--verify", "-q", f"{rev}^{{commit}}"], path) or None


def remote_url(path: Path | str) -> Optional[str]:
    url = try_run(["config", "--get", "remote.origin.url"], path)
    if url:
        return url
    remotes = (try_run(["remote"], path) or "").split()
    if remotes:
        return try_run(["config", "--get", f"remote.{remotes[0]}.url"], path) or None
    return None


def default_branch(path: Path | str) -> Optional[str]:
    ref = try_run(["symbolic-ref", "-q", "refs/remotes/origin/HEAD"], path)
    if ref:
        return ref.rsplit("/", 1)[-1]
    for name in ("main", "master", "trunk", "develop"):
        if try_run(["rev-parse", "--verify", "-q", f"refs/heads/{name}"], path):
            return name
    return None


def default_branch_ref(path: Path | str) -> Optional[str]:
    """Best ref for the default branch, preferring the remote-tracking one."""
    name = default_branch(path)
    if not name:
        return None
    for ref in (f"refs/remotes/origin/{name}", f"refs/heads/{name}"):
        if try_run(["rev-parse", "--verify", "-q", ref], path):
            return ref
    return None


def is_ancestor(path: Path | str, ancestor: str, descendant: str) -> bool:
    proc = subprocess.run(
        ["git", *BASE_ARGS, "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=str(path),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=_env(),
    )
    return proc.returncode == 0


def merge_base(path: Path | str, a: str, b: str) -> Optional[str]:
    return try_run(["merge-base", a, b], path) or None


def rev_list(path: Path | str, range_spec: str, reverse: bool = True, first_parent: bool = False,
             exclude: Sequence[str] = ()) -> List[str]:
    args = ["rev-list"]
    if reverse:
        args.append("--reverse")
    if first_parent:
        args.append("--first-parent")
    args.append(range_spec)
    args += [f"^{ref}" for ref in exclude]
    out = try_run(args, path)
    return out.split() if out else []


def commit_range(base: Optional[str], head: str) -> str:
    """``base..head``, or every ancestor of head when there is no base (an unborn branch at start)."""
    return f"{base}..{head}" if base else head


def branch_commits(path: Path | str, base: Optional[str], head: str, exclude: Sequence[str] = ()) -> List[str]:
    """Commits a branch gained between base and head, oldest first.

    That is its first-parent line, plus commits merged in from anywhere except
    ``exclude`` (normally the default branch). Merging main into a feature
    brings main's commits along, but they are not the feature's: they stay
    out, while a teammate's commits pulled from the same feature branch stay in.
    """
    spec = commit_range(base, head)
    everything = rev_list(path, spec)
    if not everything or not exclude:
        return everything
    own_line = set(rev_list(path, spec, first_parent=True))
    elsewhere = set(rev_list(path, spec, exclude=exclude))
    return [sha for sha in everything if sha in own_line or sha in elsewhere]


def upstream_refs(path: Path | str, branch: Optional[str]) -> List[str]:
    """The default branch's refs to leave out of a feature's commits; none when the feature is on it."""
    name = default_branch(path)
    if not name or branch == name:
        return []
    found = []
    for ref in (f"refs/heads/{name}", f"refs/remotes/origin/{name}"):
        if try_run(["rev-parse", "--verify", "-q", ref], path):
            found.append(ref)
    return found


def newest(path: Path | str, commits: Sequence[Optional[str]]) -> Optional[str]:
    """The commit among these that descends from all the others (None if they diverge or none is given)."""
    present = [c for c in commits if c]
    if not present:
        return None
    best = present[0]
    for candidate in present[1:]:
        if candidate == best or is_ancestor(path, candidate, best):
            continue
        if is_ancestor(path, best, candidate):
            best = candidate
        else:
            return None
    return best


def empty_tree(path: Path | str) -> str:
    """The id of the empty tree in this repository's hash (a base for diffs from nothing)."""
    out = try_run(["hash-object", "-t", "tree", "/dev/null"], path)
    return out or "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


_META_FORMAT = "%H%x1f%P%x1f%an%x1f%ae%x1f%aI%x1f%cn%x1f%cI%x1f%T%x1f%s%x1f%b"


def commit_meta(path: Path | str, sha: str) -> Optional[dict]:
    out = try_run(["show", "-s", f"--format={_META_FORMAT}", sha], path)
    if not out:
        return None
    parts = out.split("\x1f")
    while len(parts) < 10:
        parts.append("")
    return {
        "sha": parts[0],
        "parents": parts[1].split(),
        "author": parts[2],
        "author_email": parts[3],
        "authored_at": parts[4],
        "committer": parts[5],
        "committed_at": parts[6],
        "tree": parts[7],
        "subject": parts[8],
        "body": parts[9].strip(),
    }


DIFF_ARGS = ["--no-color", "--no-ext-diff", "--no-textconv", "-M", "--full-index", "-U3"]


def diff_text(path: Path | str, base: str, head_rev: Optional[str] = None) -> str:
    """Unified diff from ``base`` to ``head_rev`` (or the working tree)."""
    args = ["diff", *DIFF_ARGS, base]
    if head_rev:
        args.append(head_rev)
    return run(args, path)


def commit_patch(path: Path | str, sha: str) -> str:
    return run(["show", "--format=", "--patch", *DIFF_ARGS, sha], path)


def file_at(path: Path | str, rev: str, filepath: str) -> Optional[bytes]:
    try:
        return run_bytes(["show", f"{rev}:{filepath}"], path)
    except GitError:
        return None


def blob_id(path: Path | str, rev: str, filepath: str) -> Optional[str]:
    return try_run(["rev-parse", "--verify", "-q", f"{rev}:{filepath}"], path) or None


def tree_of(path: Path | str, rev: str) -> Optional[str]:
    return try_run(["rev-parse", "--verify", "-q", f"{rev}^{{tree}}"], path) or None


def untracked(path: Path | str) -> List[str]:
    out = try_run(["ls-files", "--others", "--exclude-standard", "-z"], path) or ""
    return [p for p in out.split("\x00") if p]


def status_porcelain(path: Path | str) -> List[Tuple[str, str]]:
    # Not try_run: stripping would eat the leading space of the first entry's status (" M path").
    try:
        out = run(["status", "--porcelain=v1", "-z", "--untracked-files=all"], path)
    except (GitError, FileNotFoundError, NotADirectoryError):
        out = ""
    entries: List[Tuple[str, str]] = []
    items = out.split("\x00")
    i = 0
    while i < len(items):
        item = items[i]
        if not item:
            i += 1
            continue
        code, file_path = item[:2], item[3:]
        entries.append((code, file_path))
        if code[0] in "RC":
            i += 1  # skip the original path of a rename
        i += 1
    return entries


def is_dirty(path: Path | str) -> bool:
    return bool(status_porcelain(path))


def patch_id(path: Path | str, patch_text: str) -> Optional[str]:
    if not patch_text.strip():
        return None
    try:
        out = run(["patch-id", "--stable"], path, input_text=patch_text)
    except GitError:
        return None
    first = out.strip().split()
    return first[0] if first else None


def refs(path: Path | str) -> Dict[str, str]:
    out = try_run(["for-each-ref", "--format=%(refname) %(objectname)"], path) or ""
    result: Dict[str, str] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2:
            result[parts[0]] = parts[1]
    return result


def worktrees(path: Path | str) -> List[dict]:
    out = try_run(["worktree", "list", "--porcelain"], path) or ""
    trees: List[dict] = []
    current: dict = {}
    for line in out.splitlines():
        if not line.strip():
            if current:
                trees.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            current = {"path": value}
        elif key == "HEAD":
            current["head"] = value
        elif key == "branch":
            current["branch"] = value.replace("refs/heads/", "", 1)
        elif key == "bare":
            current["bare"] = True
    if current:
        trees.append(current)
    return trees


def hash_files(path: Path | str, files: Sequence[str]) -> Dict[str, Optional[str]]:
    """Blob ids of working-tree files, computed without writing anything into the repository."""
    out: Dict[str, Optional[str]] = {}
    present = [f for f in files if (Path(path) / f).is_file()]
    for f in files:
        out[f] = None
    if present:
        try:
            ids = run(["hash-object", "--no-filters", "--stdin-paths"], path, input_text="\n".join(present) + "\n").split()
        except GitError:
            return out
        out.update(dict(zip(present, ids)))
    return out


def changed_paths(path: Path | str, base: str, head_rev: str) -> List[str]:
    """Every path whose content differs between two commits, renames as delete plus add."""
    out = try_run(["diff", "--name-only", "--no-renames", "--no-ext-diff", "-z", base, head_rev], path) or ""
    return [p for p in out.split("\x00") if p]


def name_status(path: Path | str, base: str, head_rev: Optional[str] = None) -> List[Tuple[str, str]]:
    args = ["diff", "--name-status", "-M", "--no-ext-diff", base]
    if head_rev:
        args.append(head_rev)
    out = try_run(args, path) or ""
    rows: List[Tuple[str, str]] = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            rows.append((parts[0], parts[-1]))
    return rows

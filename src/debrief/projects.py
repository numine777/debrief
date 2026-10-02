"""Projects: identity, the host-local registry, and ``debrief init``.

A project id is derived from the repository's normalized remote URL, so every
clone, worktree and host agrees on it without anything written into the repo.
A repository without a remote falls back to a path-based id.

The registry (``<archive>/.registry.json``) maps project ids to repository
paths on *this* host. It never syncs, because paths differ between hosts.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import archive, gitutil, paths, residency, util


def normalize_remote(url: str) -> str:
    """Canonical ``host/path`` form of a git remote URL."""
    text = url.strip()
    text = re.sub(r"^[a-z+]+://", "", text)          # scheme
    text = re.sub(r"^[^@/]+@", "", text)              # user@
    if "/" not in text.split(":", 1)[0] and ":" in text and not re.match(r"^[^:]+:\d+/", text):
        text = text.replace(":", "/", 1)              # scp-like host:path
    text = re.sub(r"^([^/:]+):\d+/", r"\1/", text)     # host:port/
    text = text.rstrip("/")
    if text.endswith(".git"):
        text = text[:-4]
    return text.lower()


def project_id_for(remote: Optional[str], common_dir: Optional[Path]) -> str:
    if remote:
        normalized = normalize_remote(remote)
        slug = re.sub(r"[^a-z0-9._-]+", "-", normalized).strip("-")
        return slug[:120] or "project"
    base = common_dir.parent.name if common_dir and common_dir.name == ".git" else (
        common_dir.name.removesuffix(".git") if common_dir else "repo")
    return f"local-{util.slugify(base)}-{util.short_hash(str(common_dir), 6)}"


def display_name_for(remote: Optional[str], common_dir: Optional[Path]) -> str:
    if remote:
        return normalize_remote(remote).rsplit("/", 1)[-1]
    if common_dir is None:
        return "repo"
    return common_dir.parent.name if common_dir.name == ".git" else common_dir.name.removesuffix(".git")


# --- registry --------------------------------------------------------------


def load_registry(root: Optional[Path] = None) -> dict:
    data = util.read_json(paths.registry_path(root), None)
    if not isinstance(data, dict):
        data = {"projects": {}}
    data.setdefault("projects", {})
    return data


def save_registry(data: dict, root: Optional[Path] = None) -> None:
    util.write_json(paths.registry_path(root), data)


def registered_projects(root: Optional[Path] = None) -> Dict[str, dict]:
    return load_registry(root)["projects"]


def find_project(repo_path: Path | str, root: Optional[Path] = None) -> Optional[Tuple[str, dict]]:
    """Return (project id, registry entry) for the repo containing ``repo_path``."""
    common = gitutil.common_dir(repo_path)
    if common is None:
        return None
    for pid, entry in registered_projects(root).items():
        for candidate in entry.get("common_dirs", []):
            try:
                if Path(candidate).resolve() == common:
                    return pid, entry
            except OSError:
                continue
    return None


def repo_paths(project_id: str, root: Optional[Path] = None) -> List[Path]:
    entry = registered_projects(root).get(project_id) or {}
    return [Path(p) for p in entry.get("common_dirs", []) if Path(p).exists()]


def repo_workdir(project_id: str, root: Optional[Path] = None) -> Optional[Path]:
    """A directory git commands can run in for this project on this host."""
    for common in repo_paths(project_id, root):
        if common.name == ".git":
            return common.parent
        return common  # bare repository: git runs fine with cwd at the git dir
    return None


class InitResult:
    def __init__(self, project_id: str, project_dir: Path, created: bool, name: str):
        self.project_id = project_id
        self.project_dir = project_dir
        self.created = created
        self.name = name


def init_project(repo: Path | str, remote: Optional[str] = None, name: Optional[str] = None,
                 root: Optional[Path] = None) -> InitResult:
    """Register a repository. Writes nothing into the repository itself."""
    repo = Path(repo).expanduser()
    common = gitutil.common_dir(repo)
    if common is None:
        raise ValueError(f"{repo} is not inside a git repository")
    archive_root = root or paths.archive_root()
    residency.check_archive_path(archive_root)
    origin = gitutil.remote_url(repo)
    project_id = project_id_for(origin, common)
    project_path = paths.project_dir(project_id, archive_root)
    created = not project_path.exists()
    archive.ensure_repo(project_path)

    meta_path = project_path / "project.json"
    meta = util.read_json(meta_path, {}) or {}
    meta.setdefault("project_id", project_id)
    meta.setdefault("created_at", util.now_iso())
    meta["remote_url"] = origin
    meta["display_name"] = name or meta.get("display_name") or display_name_for(origin, common)
    util.write_json(meta_path, meta)
    if remote:
        archive.set_remote(project_path, remote)
    with archive.lock(project_path):
        archive.commit(project_path, f"Register {meta['display_name']}", ["project.json", ".gitignore"])

    registry = load_registry(archive_root)
    entry = registry["projects"].setdefault(project_id, {})
    dirs = entry.setdefault("common_dirs", [])
    if str(common) not in dirs:
        dirs.append(str(common))
    entry["display_name"] = meta["display_name"]
    entry["registered_at"] = entry.get("registered_at") or util.now_iso()
    save_registry(registry, archive_root)
    return InitResult(project_id, project_path, created, meta["display_name"])


def project_meta(project_id: str, root: Optional[Path] = None) -> dict:
    return util.read_json(paths.project_dir(project_id, root) / "project.json", {}) or {}


def list_projects(root: Optional[Path] = None) -> List[str]:
    base = paths.projects_dir(root)
    if not base.exists():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_dir() and not p.name.startswith("."))

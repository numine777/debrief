"""Where Debrief keeps things.

Everything lives outside the repositories Debrief supports (goal G7):

* the archive, ``$AI_SESSIONS_DIR`` or ``~/.local/share/ai-sessions``: records,
  evidence and comments, one git repository per project;
* the tool home, ``~/.local/share/debrief``: the installed zipapp;
* the config, ``~/.config/debrief/config``.
"""

from __future__ import annotations

import os
from pathlib import Path


def _xdg(var: str, default: str) -> Path:
    value = os.environ.get(var)
    if value:
        return Path(value).expanduser()
    return Path.home() / default


def archive_root() -> Path:
    override = os.environ.get("AI_SESSIONS_DIR")
    if override:
        return Path(override).expanduser()
    return _xdg("XDG_DATA_HOME", ".local/share") / "ai-sessions"


def config_dir() -> Path:
    override = os.environ.get("DEBRIEF_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    return _xdg("XDG_CONFIG_HOME", ".config") / "debrief"


def config_file() -> Path:
    return config_dir() / "config"


def tool_home() -> Path:
    override = os.environ.get("DEBRIEF_HOME")
    if override:
        return Path(override).expanduser()
    return _xdg("XDG_DATA_HOME", ".local/share") / "debrief"


def user_bin_dir() -> Path:
    override = os.environ.get("DEBRIEF_BIN_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local" / "bin"


def projects_dir(root: Path | None = None) -> Path:
    return (root or archive_root()) / "projects"


def project_dir(project_id: str, root: Path | None = None) -> Path:
    return projects_dir(root) / project_id


def feature_dir(project_id: str, feature_id: str, root: Path | None = None) -> Path:
    return project_dir(project_id, root) / "features" / feature_id


def registry_path(root: Path | None = None) -> Path:
    """Host-local map of projects to repository paths. Never synced."""
    return (root or archive_root()) / ".registry.json"


def index_path(root: Path | None = None) -> Path:
    return (root or archive_root()) / ".index.sqlite"


def viewer_state_dir(root: Path | None = None) -> Path:
    """Host-local viewer state: private comments, review marks. Never synced."""
    return (root or archive_root()) / ".viewer"

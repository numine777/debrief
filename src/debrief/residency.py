"""Data residency guards (goal G6).

Debrief only talks to hosts on the allowlist in ``~/.config/debrief/config``,
refuses to keep its archive inside cloud-synced folders, and never pushes a
project repository. These checks are enforced in code, not left to convention.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urlparse

from . import config as _config

_SCP_LIKE = re.compile(r"^(?:[^@/\s]+@)?(?P<host>[^:/\s]+):(?!//)")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class ResidencyError(RuntimeError):
    """Raised when an operation would send data somewhere not allowed."""


def host_of(url: str) -> Optional[str]:
    """Return the network host a git remote or HTTP URL points at.

    ``None`` means the location is local (a path or ``file://`` URL).
    """
    text = (url or "").strip()
    if not text:
        return None
    if "://" in text:
        parsed = urlparse(text)
        if parsed.scheme == "file":
            return None
        return (parsed.hostname or "").lower() or None
    match = _SCP_LIKE.match(text)
    if match and not text.startswith(("/", ".", "~")):
        host = match.group("host").lower()
        # Windows drive letters like C:\ are local paths, not hosts.
        if len(host) == 1:
            return None
        return host
    return None


def host_allowed(host: Optional[str], allow: Iterable[str]) -> bool:
    if host is None or host in _LOCAL_HOSTS:
        return True
    for pattern in allow:
        pattern = pattern.strip().lower()
        if not pattern:
            continue
        pattern = pattern.split(":")[0] if pattern.count(":") == 1 else pattern
        if host == pattern or fnmatch.fnmatch(host, pattern):
            return True
    return False


def check_url(url: str, purpose: str, cfg: Optional[_config.Config] = None) -> None:
    """Raise ResidencyError unless ``url`` is local or on the allowlist."""
    cfg = cfg or _config.load()
    host = host_of(url)
    if not host_allowed(host, cfg.allow_hosts):
        raise ResidencyError(
            f"{purpose}: {host} is not on the residency allowlist. "
            f"Add it to [residency] allow_hosts in {cfg.path} if it is an approved host."
        )


_CLOUD_COMPONENTS = {
    "dropbox": "Dropbox",
    "google drive": "Google Drive",
    "my drive": "Google Drive",
    "icloud drive": "iCloud Drive",
    "box sync": "Box",
    "pcloud drive": "pCloud",
}


def cloud_sync_provider(path: Path) -> Optional[str]:
    """Name the cloud-sync service a path lives under, if any."""
    try:
        resolved = Path(path).expanduser().resolve()
    except OSError:
        resolved = Path(path).expanduser()
    text = str(resolved).replace("\\", "/")
    if "/Library/Mobile Documents/" in text + "/":
        return "iCloud Drive"
    if "/Library/CloudStorage/" in text + "/":
        return "a macOS cloud storage provider"
    for part in resolved.parts:
        lowered = part.lower()
        if lowered in _CLOUD_COMPONENTS:
            return _CLOUD_COMPONENTS[lowered]
        if lowered.startswith("onedrive"):
            return "OneDrive"
    return None


def check_archive_path(path: Path) -> None:
    provider = cloud_sync_provider(path)
    if provider:
        raise ResidencyError(
            f"The archive at {path} is inside a folder synced by {provider}. "
            "Debrief refuses cloud-synced locations; set AI_SESSIONS_DIR to a local path."
        )

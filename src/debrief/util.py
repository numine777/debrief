"""Small helpers shared across Debrief: time, JSON files, locking, slugs."""

from __future__ import annotations

import contextlib
import datetime as _dt
import hashlib
import json
import os
import re
import socket
import tempfile
from pathlib import Path
from typing import Any, Iterator


def utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def now_iso(when: _dt.datetime | None = None) -> str:
    """UTC timestamp to the second, e.g. ``2026-10-02T19:30:00Z``."""
    return (when or utcnow()).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_minute(when: _dt.datetime | None = None) -> str:
    """UTC timestamp to the minute, the journal heading format."""
    return (when or utcnow()).strftime("%Y-%m-%dT%H:%MZ")


def parse_iso(value: str | None) -> _dt.datetime | None:
    """Parse the timestamps Debrief writes (and common ISO variants).

    ``datetime.fromisoformat`` only accepts a trailing ``Z`` from Python 3.11,
    so normalize it here to keep 3.9 support.
    """
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # Minute-precision journal stamps: 2026-10-02T19:41+00:00
    if re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}\+00:00$", text):
        text = text.replace("+00:00", ":00+00:00")
    try:
        parsed = _dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError:
        return default


def write_json(path: Path, data: Any) -> None:
    """Write JSON atomically so readers never see a half-written file."""
    write_text(path, json.dumps(data, indent=2, sort_keys=False, ensure_ascii=False) + "\n")


def write_text(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def write_bytes(path: Path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """Advisory exclusive lock; a no-op where fcntl is unavailable."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl  # POSIX only; Windows users run Debrief under WSL.
    except ImportError:  # pragma: no cover
        yield
        return
    with open(path, "a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str, fallback: str = "item") -> str:
    slug = _SLUG_RE.sub("-", text.lower()).strip("-")
    return slug or fallback


def short_hash(text: str, length: int = 12) -> str:
    return hashlib.sha1(text.encode("utf-8", "surrogateescape")).hexdigest()[:length]


def hostname() -> str:
    return os.environ.get("DEBRIEF_HOSTNAME") or socket.gethostname().split(".")[0] or "host"


def feature_id_for_branch(branch: str) -> str:
    """Feature id = branch name with ``/`` replaced by ``--``."""
    cleaned = branch.strip().replace("/", "--")
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", cleaned).strip("-.")
    return cleaned or "detached"


def human_count(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")

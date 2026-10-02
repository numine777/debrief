"""Read files shipped inside the package, from a source tree or the zipapp."""

from __future__ import annotations

import os
import pkgutil
import zipfile
from pathlib import Path
from typing import List

PKG_DIR = Path(__file__).resolve().parent


def _zip_archive() -> str | None:
    loader = globals().get("__loader__")
    archive = getattr(loader, "archive", None)
    return archive if archive and zipfile.is_zipfile(archive) else None


def read_bytes(rel: str) -> bytes:
    data = pkgutil.get_data("debrief", rel)
    if data is None:
        raise FileNotFoundError(rel)
    return data


def read_text(rel: str) -> str:
    return read_bytes(rel).decode("utf-8")


def list_files(prefix: str) -> List[str]:
    """Return package-relative paths of every file under ``prefix``."""
    prefix = prefix.strip("/")
    archive = _zip_archive()
    if archive:
        base = f"debrief/{prefix}/"
        with zipfile.ZipFile(archive) as zf:
            return sorted(
                name[len("debrief/"):]
                for name in zf.namelist()
                if name.startswith(base) and not name.endswith("/")
            )
    root = PKG_DIR / prefix
    found: List[str] = []
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if name.endswith((".pyc",)) or name == ".DS_Store":
                continue
            full = Path(dirpath) / name
            found.append(str(full.relative_to(PKG_DIR)).replace(os.sep, "/"))
    return sorted(found)


def running_from_zip() -> bool:
    return _zip_archive() is not None


def zip_path() -> str | None:
    return _zip_archive()

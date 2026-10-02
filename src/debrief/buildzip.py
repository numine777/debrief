"""Build ``debrief.pyz``: the package as one runnable zipapp.

The archive is reproducible: entries are sorted and carry a fixed timestamp, so
the same source always produces the same bytes.
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path
from typing import Optional

from . import resources

SHEBANG = b"#!/usr/bin/env python3\n"
MAIN = "import sys\nfrom debrief.cli import main\nsys.exit(main())\n"
_FIXED_TIME = (2020, 1, 1, 0, 0, 0)
_SKIP_DIRS = {"__pycache__"}
_SKIP_SUFFIXES = (".pyc", ".pyo")
_SKIP_NAMES = {".DS_Store"}


def _entry(name: str, executable: bool = False) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=_FIXED_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    mode = 0o755 if executable else 0o644
    info.external_attr = (stat.S_IFREG | mode) << 16
    return info


def build(target: Path, package_dir: Optional[Path] = None) -> Path:
    """Write the zipapp to ``target`` and return its path."""
    package_dir = Path(package_dir or resources.PKG_DIR)
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    files = []
    for root, dirs, names in os.walk(package_dir):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS)
        for name in names:
            if name in _SKIP_NAMES or name.endswith(_SKIP_SUFFIXES):
                continue
            full = Path(root) / name
            files.append((str(full.relative_to(package_dir)).replace(os.sep, "/"), full))
    files.sort()
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".pyz", dir=str(target.parent))
    os.close(fd)
    try:
        with open(tmp, "wb") as handle:
            handle.write(SHEBANG)
            with zipfile.ZipFile(handle, "w") as zf:
                zf.writestr(_entry("__main__.py"), MAIN)
                for rel, full in files:
                    zf.writestr(_entry(f"debrief/{rel}"), full.read_bytes())
        os.chmod(tmp, 0o755)
        os.replace(tmp, target)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return target


def install_copy(target: Path) -> Path:
    """Put a zipapp at ``target``: copy the running one, or build from source."""
    running = resources.zip_path()
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if running:
        if Path(running).resolve() == target.resolve():
            return target
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".pyz", dir=str(target.parent))
        os.close(fd)
        shutil.copyfile(running, tmp)
        os.chmod(tmp, 0o755)
        os.replace(tmp, target)
        return target
    return build(target)

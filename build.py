#!/usr/bin/env python3
"""Build dist/debrief.pyz, the single-file distribution of Debrief."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from debrief import __version__, buildzip  # noqa: E402


def main() -> int:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "dist" / "debrief.pyz"
    buildzip.build(target, ROOT / "src" / "debrief")
    size = target.stat().st_size
    print(f"built {target} (debrief {__version__}, {size / 1024:.0f} KiB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/bin/sh
# @NAME@: installed by `debrief install` (Debrief @VERSION@). Upgrades overwrite this file.
PYZ="${DEBRIEF_PYZ:-@PYZ@}"
if [ ! -f "$PYZ" ]; then
  echo "@NAME@: $PYZ is missing; run \`debrief install\` again." >&2
  exit 127
fi
for py in python3 python3.13 python3.12 python3.11 python3.10 python3.9 python; do
  if command -v "$py" >/dev/null 2>&1 && "$py" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
    exec "$py" "$PYZ" @SUBCOMMAND@"$@"
  fi
done
echo "@NAME@: needs Python 3.9 or later on PATH" >&2
exit 127

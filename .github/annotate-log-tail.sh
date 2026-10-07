#!/usr/bin/env bash
# Repeats the end of a failed step's log as error annotations, which show on
# the run's summary page and through the checks API without opening the logs.
# Usage: annotate-log-tail.sh LOG TITLE
set -euo pipefail
tail -n 200 "$1" | python3 -c '
import sys
title = sys.argv[1]
lines = sys.stdin.read().splitlines()
size = 40
for start in range(0, len(lines), size):
    chunk = "\n".join(lines[start:start + size])
    chunk = chunk.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::error title={title} ({start // size + 1})::{chunk}")
' "$2"

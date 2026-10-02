---
id: SYSTEM-ID
title: Human-readable name
change: new
depends_on: []
anchors:
  - {path: path/to/file.py, symbol: ClassName.method, role: core}
critical_paths:
  - id: CRITICAL-PATH-ID
    kind: loop
    anchor: {path: path/to/file.py, symbol: ClassName.method}
    invariant: "What must always hold, such as when the loop terminates."
decisions: []
---

## Purpose

What this mechanism is for, in two or three sentences.

## Change

What this feature did to it, in one to three sentences. "New" for a new system.

## How it works

Control and data flow as the code now stands: entry points, the main path,
where state lives, how errors are handled.

## Limitations

Known limits, assumptions and edge cases it does not handle.

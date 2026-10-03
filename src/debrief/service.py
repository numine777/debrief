"""``debrief service``: run ``debrief serve --watch`` as a user service.

systemd user unit on Linux, a launchd agent on macOS. Install writes the file
and prints the one command that starts it; it never enables services silently.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from . import paths, util

UNIT_NAME = "debrief.service"
PLIST_NAME = "dev.debrief.serve.plist"


def pyz_path() -> Path:
    return paths.tool_home() / "debrief.pyz"


def systemd_unit() -> str:
    python = shutil.which("python3") or "/usr/bin/python3"
    return f"""[Unit]
Description=Debrief viewer and watcher (127.0.0.1 only)
After=default.target

[Service]
ExecStart={python} {pyz_path()} serve --watch
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
"""


def launchd_plist() -> str:
    python = shutil.which("python3") or "/usr/bin/python3"
    log = paths.tool_home() / "serve.log"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>dev.debrief.serve</string>
  <key>ProgramArguments</key>
  <array><string>{python}</string><string>{pyz_path()}</string><string>serve</string><string>--watch</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
"""


def target() -> tuple:
    if sys.platform == "darwin":
        path = Path.home() / "Library" / "LaunchAgents" / PLIST_NAME
        return path, launchd_plist(), f"launchctl load -w {path}", f"launchctl unload -w {path}"
    path = Path.home() / ".config" / "systemd" / "user" / UNIT_NAME
    return (path, systemd_unit(), "systemctl --user daemon-reload && systemctl --user enable --now debrief",
            "systemctl --user disable --now debrief")


def cli(args) -> int:
    path, text, start_cmd, stop_cmd = target()
    if args.action == "print":
        print(f"# {path}")
        print(text)
        return 0
    if args.action == "install":
        if not pyz_path().exists():
            print(f"{pyz_path()} is missing; run `debrief install` first.")
            return 1
        util.write_text(path, text)
        print(f"Wrote {path}. Start it with:\n  {start_cmd}")
        return 0
    if path.exists():
        print(f"Stop it first with:\n  {stop_cmd}")
        path.unlink()
        print(f"Removed {path}.")
    else:
        print("No Debrief service file is installed.")
    return 0

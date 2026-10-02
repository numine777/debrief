"""User configuration in ``~/.config/debrief/config`` (INI format).

INI keeps the file editable by hand and parseable with the standard library on
Python 3.9 (``tomllib`` arrived in 3.11). Example::

    [residency]
    # Hosts Debrief may contact: archive remotes, the hub, compaction endpoints.
    allow_hosts = git.corp.example, *.corp.example

    [viewer]
    port = 7319

    [experimental]
    compaction = false

    [compaction]
    provider = intranet

    [provider.intranet]
    api = openai                # or anthropic
    base_url = https://models.corp.example/openai/v1
    model = gpt-5-mini
    api_key_env = DEBRIEF_COMPACTION_KEY
    auth_header = api-key       # authorization (default), api-key or x-api-key
"""

from __future__ import annotations

import configparser
from pathlib import Path
from typing import Dict, List, Optional

from . import paths

DEFAULT_PORT = 7319


class Config:
    def __init__(self, parser: configparser.ConfigParser, path: Path):
        self._parser = parser
        self.path = path

    def get(self, section: str, key: str, default: Optional[str] = None) -> Optional[str]:
        try:
            value = self._parser.get(section, key)
        except (configparser.NoSectionError, configparser.NoOptionError):
            return default
        return value.strip()

    def get_bool(self, section: str, key: str, default: bool = False) -> bool:
        value = self.get(section, key)
        if value is None:
            return default
        return value.lower() in {"1", "true", "yes", "on"}

    def get_int(self, section: str, key: str, default: int) -> int:
        value = self.get(section, key)
        try:
            return int(value) if value is not None else default
        except ValueError:
            return default

    def get_list(self, section: str, key: str) -> List[str]:
        value = self.get(section, key) or ""
        items = [part.strip() for part in value.replace("\n", ",").split(",")]
        return [item for item in items if item]

    def section(self, name: str) -> Dict[str, str]:
        if not self._parser.has_section(name):
            return {}
        return {k: v.strip() for k, v in self._parser.items(name)}

    def sections(self) -> List[str]:
        return self._parser.sections()

    @property
    def allow_hosts(self) -> List[str]:
        return self.get_list("residency", "allow_hosts")

    @property
    def port(self) -> int:
        return self.get_int("viewer", "port", DEFAULT_PORT)


def load(path: Optional[Path] = None) -> Config:
    path = Path(path) if path else paths.config_file()
    parser = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    if path.exists():
        parser.read(path, encoding="utf-8")
    return Config(parser, path)


DEFAULT_TEXT = """\
# Debrief configuration. See `debrief help config`.

[residency]
# Hosts Debrief may contact: archive remotes, the hub, compaction endpoints.
# Local paths and localhost are always allowed. Wildcards like *.corp.example work.
allow_hosts =

[viewer]
port = 7319

[sync]
# Seconds between automatic archive pulls by `debrief-session now` and the watcher.
pull_interval = 120

[experimental]
# Model-written compaction for `debrief squash`. Off unless set to true.
compaction = false
"""


def ensure_default() -> Path:
    path = paths.config_file()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(DEFAULT_TEXT, encoding="utf-8")
    return path

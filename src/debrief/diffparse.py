"""Parse unified diffs (as ``git diff --full-index`` writes them) and map lines.

Each hunk gets a content hash over its path and changed lines, not its line
numbers, so a review mark survives unrelated edits above it and clears when
the hunk's own code changes.
"""

from __future__ import annotations

import difflib
import hashlib
import re
from typing import Dict, Iterable, List, Optional, Tuple

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$")
_INDEX_RE = re.compile(r"^index ([0-9a-f]+)\.\.([0-9a-f]+)(?: (\d+))?$")
_NULL_SHA = re.compile(r"^0+$")


def _unquote(path: str) -> str:
    """Undo git's C-style quoting of unusual paths."""
    if not (path.startswith('"') and path.endswith('"')):
        return path
    body = path[1:-1]
    out = bytearray()
    i = 0
    escapes = {"n": 10, "t": 9, "r": 13, '"': 34, "\\": 92, "a": 7, "b": 8, "f": 12, "v": 11}
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            if nxt in escapes:
                out.append(escapes[nxt])
                i += 2
                continue
            if re.match(r"[0-7]{3}", body[i + 1:i + 4]):
                out.append(int(body[i + 1:i + 4], 8))
                i += 4
                continue
        out.extend(ch.encode("utf-8"))
        i += 1
    return out.decode("utf-8", "replace")


def _strip_prefix(path: str) -> Optional[str]:
    path = _unquote(path.strip())
    if path == "/dev/null":
        return None
    if path[:2] in ("a/", "b/"):
        return path[2:]
    return path


class Hunk:
    __slots__ = ("old_start", "old_len", "new_start", "new_len", "section", "lines", "id")

    def __init__(self, old_start: int, old_len: int, new_start: int, new_len: int, section: str):
        self.old_start = old_start
        self.old_len = old_len
        self.new_start = new_start
        self.new_len = new_len
        self.section = section
        self.lines: List[Tuple[str, str]] = []  # (tag, text) with tag in " +-\\"
        self.id = ""

    @property
    def additions(self) -> int:
        return sum(1 for tag, _ in self.lines if tag == "+")

    @property
    def deletions(self) -> int:
        return sum(1 for tag, _ in self.lines if tag == "-")

    def new_range(self) -> Tuple[int, int]:
        """Inclusive new-side line range; a pure deletion spans the lines around it."""
        if self.new_len > 0:
            return self.new_start, self.new_start + self.new_len - 1
        return max(self.new_start, 1), self.new_start + 1

    def old_range(self) -> Tuple[int, int]:
        if self.old_len > 0:
            return self.old_start, self.old_start + self.old_len - 1
        return max(self.old_start, 1), self.old_start + 1

    def numbered(self) -> Iterable[Tuple[str, Optional[int], Optional[int], str]]:
        """Yield (tag, old line, new line, text) for every line in the hunk."""
        old, new = self.old_start, self.new_start
        for tag, text in self.lines:
            if tag == " ":
                yield tag, old, new, text
                old += 1
                new += 1
            elif tag == "-":
                yield tag, old, None, text
                old += 1
            elif tag == "+":
                yield tag, None, new, text
                new += 1
            else:
                yield tag, None, None, text

    def whitespace_only(self) -> bool:
        removed = "".join("".join(t.split()) for tag, t in self.lines if tag == "-")
        added = "".join("".join(t.split()) for tag, t in self.lines if tag == "+")
        return bool(self.lines) and removed == added

    def to_dict(self, with_lines: bool = True) -> dict:
        data = {
            "id": self.id,
            "old_start": self.old_start,
            "old_len": self.old_len,
            "new_start": self.new_start,
            "new_len": self.new_len,
            "section": self.section,
            "additions": self.additions,
            "deletions": self.deletions,
        }
        if with_lines:
            data["lines"] = [[tag, text] for tag, text in self.lines]
        return data


class FilePatch:
    def __init__(self) -> None:
        self.old_path: Optional[str] = None
        self.new_path: Optional[str] = None
        self.status = "M"
        self.binary = False
        self.old_blob: Optional[str] = None
        self.new_blob: Optional[str] = None
        self.old_mode: Optional[str] = None
        self.new_mode: Optional[str] = None
        self.similarity: Optional[int] = None
        self.hunks: List[Hunk] = []

    @property
    def path(self) -> str:
        return self.new_path or self.old_path or ""

    @property
    def additions(self) -> int:
        return sum(h.additions for h in self.hunks)

    @property
    def deletions(self) -> int:
        return sum(h.deletions for h in self.hunks)

    def to_dict(self, with_lines: bool = True) -> dict:
        return {
            "path": self.path,
            "old_path": self.old_path,
            "new_path": self.new_path,
            "status": self.status,
            "binary": self.binary,
            "old_blob": self.old_blob,
            "new_blob": self.new_blob,
            "old_mode": self.old_mode,
            "new_mode": self.new_mode,
            "similarity": self.similarity,
            "additions": self.additions,
            "deletions": self.deletions,
            "hunks": [h.to_dict(with_lines) for h in self.hunks],
        }


def hunk_id(path: str, hunk: Hunk) -> str:
    digest = hashlib.sha1()
    digest.update(path.encode("utf-8", "surrogateescape"))
    for tag, text in hunk.lines:
        if tag in "+-":
            digest.update(b"\n" + tag.encode() + text.encode("utf-8", "surrogateescape"))
    return digest.hexdigest()[:12]


def parse(text: str) -> List[FilePatch]:
    files: List[FilePatch] = []
    current: Optional[FilePatch] = None
    hunk: Optional[Hunk] = None
    old_left = new_left = 0
    for line in text.split("\n"):
        if hunk is not None and (old_left > 0 or new_left > 0):
            if line.startswith(("+", "-", " ")) or line == "":
                tag = line[:1] or " "
                body = line[1:]
                hunk.lines.append((tag, body))
                if tag == " ":
                    old_left -= 1
                    new_left -= 1
                elif tag == "-":
                    old_left -= 1
                else:
                    new_left -= 1
                continue
            if line.startswith("\\"):
                hunk.lines.append(("\\", line[2:] if len(line) > 1 else ""))
                continue
        if hunk is not None and line.startswith("\\"):
            hunk.lines.append(("\\", line[2:]))
            continue
        if line.startswith("diff --git ") or line.startswith("diff --cc ") or line.startswith("diff --combined "):
            current = FilePatch()
            files.append(current)
            hunk = None
            rest = line.split(" ", 2)[2] if line.startswith("diff --git ") else line.split(" ", 2)[-1]
            if line.startswith("diff --git "):
                match = re.match(r'^("(?:[^"\\]|\\.)*"|\S+) ("(?:[^"\\]|\\.)*"|\S+)$', rest)
                if match:
                    current.old_path = _strip_prefix(match.group(1))
                    current.new_path = _strip_prefix(match.group(2))
                else:
                    half = rest[2:].split(" b/", 1)
                    current.old_path = half[0]
                    current.new_path = half[1] if len(half) > 1 else half[0]
            continue
        if current is None:
            continue
        if line.startswith("@@"):
            match = _HUNK_RE.match(line)
            if not match:
                continue
            old_start, old_len = int(match.group(1)), int(match.group(2) if match.group(2) is not None else 1)
            new_start, new_len = int(match.group(3)), int(match.group(4) if match.group(4) is not None else 1)
            hunk = Hunk(old_start, old_len, new_start, new_len, match.group(5).strip())
            current.hunks.append(hunk)
            old_left, new_left = old_len, new_len
            continue
        if line.startswith("--- "):
            path = _strip_prefix(line[4:].split("\t")[0])
            if path is None:
                current.status = "A"
                current.old_path = None
            else:
                current.old_path = path
            continue
        if line.startswith("+++ "):
            path = _strip_prefix(line[4:].split("\t")[0])
            if path is None:
                current.status = "D"
                current.new_path = None
            else:
                current.new_path = path
            continue
        if line.startswith("new file mode "):
            current.status = "A"
            current.new_mode = line.split()[-1]
            current.old_path = None
        elif line.startswith("deleted file mode "):
            current.status = "D"
            current.old_mode = line.split()[-1]
            current.new_path = None
        elif line.startswith("old mode "):
            current.old_mode = line.split()[-1]
        elif line.startswith("new mode "):
            current.new_mode = line.split()[-1]
        elif line.startswith("rename from "):
            current.status = "R"
            current.old_path = _unquote(line[len("rename from "):])
        elif line.startswith("rename to "):
            current.status = "R"
            current.new_path = _unquote(line[len("rename to "):])
        elif line.startswith("copy from "):
            current.status = "C"
            current.old_path = _unquote(line[len("copy from "):])
        elif line.startswith("copy to "):
            current.status = "C"
            current.new_path = _unquote(line[len("copy to "):])
        elif line.startswith("similarity index "):
            try:
                current.similarity = int(line.split()[-1].rstrip("%"))
            except ValueError:
                pass
        elif line.startswith("index "):
            match = _INDEX_RE.match(line)
            if match:
                current.old_blob = None if _NULL_SHA.match(match.group(1)) else match.group(1)
                current.new_blob = None if _NULL_SHA.match(match.group(2)) else match.group(2)
                if match.group(3):
                    current.old_mode = current.old_mode or match.group(3)
                    current.new_mode = current.new_mode or match.group(3)
        elif line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            current.binary = True
    for patch in files:
        if patch.status == "A":
            patch.old_blob = None
        if patch.status == "D":
            patch.new_blob = None
        for h in patch.hunks:
            h.id = hunk_id(patch.path, h)
    return files


# --- line mapping ------------------------------------------------------------------


def line_map(old_text: str, new_text: str) -> Dict[int, int]:
    """Map 1-based line numbers of ``old_text`` to ``new_text`` where unchanged."""
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    mapping: Dict[int, int] = {}
    for tag, i1, i2, j1, _j2 in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(i2 - i1):
                mapping[i1 + offset + 1] = j1 + offset + 1
    return mapping


def map_range(mapping: Dict[int, int], start: int, end: int) -> Optional[Tuple[int, int]]:
    """Map an inclusive range through ``line_map``; None if no line survives."""
    mapped = [mapping[n] for n in range(start, end + 1) if n in mapping]
    if not mapped:
        return None
    return min(mapped), max(mapped)


def ranges_overlap(a: Tuple[int, int], b: Tuple[int, int]) -> bool:
    return a[0] <= b[1] and b[0] <= a[1]


VENDORED_PATTERNS = ("vendor/**", "**/vendor/**", "third_party/**", "**/third_party/**", "**/node_modules/**",
                     "external/**", "**/_vendor/**")
GENERATED_PATTERNS = (
    "*.min.js", "*.min.css", "*.map", "*_pb2.py", "*_pb2_grpc.py", "*.pb.go", "*.pb.cc", "*.pb.h",
    "**/generated/**", "**/__generated__/**", "*.generated.*", "*.g.dart",
)
LOCKFILE_NAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Pipfile.lock", "Cargo.lock",
    "go.sum", "Gemfile.lock", "composer.lock", "uv.lock", "MODULE.bazel.lock", "flake.lock", "bun.lockb",
}


def classify_noise(path: str) -> Optional[str]:
    """'lockfile', 'generated', 'vendored' or None: files whose hunks collapse by default."""
    from .records import glob_match

    name = path.rsplit("/", 1)[-1]
    if name in LOCKFILE_NAMES:
        return "lockfile"
    for pattern in VENDORED_PATTERNS:
        if glob_match(pattern, path):
            return "vendored"
    for pattern in GENERATED_PATTERNS:
        if glob_match(pattern, name) or glob_match(pattern, path):
            return "generated"
    return None

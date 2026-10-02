"""Find symbols (functions, classes, sections) and their line ranges in a file.

Python and Starlark use the ``ast`` module. Other languages use line regexes
with brace matching or indentation to find where a definition ends: good
enough to resolve an anchor such as ``RetryQueue.drain`` to a line range, and
fully offline. Anything unresolved is reported as stale, never guessed.
"""

from __future__ import annotations

import ast
import re
from typing import Callable, Dict, List, Optional, Tuple


class Symbol:
    __slots__ = ("name", "start", "end", "kind")

    def __init__(self, name: str, start: int, end: int, kind: str):
        self.name = name
        self.start = start
        self.end = max(start, end)
        self.kind = kind

    def to_dict(self) -> dict:
        return {"name": self.name, "start": self.start, "end": self.end, "kind": self.kind}

    def __repr__(self) -> str:  # pragma: no cover
        return f"Symbol({self.name!r}, {self.start}, {self.end}, {self.kind!r})"


# --- Python and Starlark -----------------------------------------------------------


def _python(text: str, starlark: bool = False) -> List[Symbol]:
    tree = ast.parse(text)
    out: List[Symbol] = []

    def start_of(node) -> int:
        decorators = [d.lineno for d in getattr(node, "decorator_list", [])]
        return min(decorators + [node.lineno])

    def visit(node, prefix: str, in_function: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}.{child.name}" if prefix else child.name
                kind = "class" if isinstance(child, ast.ClassDef) else ("method" if prefix and not in_function else "function")
                out.append(Symbol(name, start_of(child), getattr(child, "end_lineno", child.lineno), kind))
                visit(child, name, not isinstance(child, ast.ClassDef))
            elif isinstance(child, (ast.Assign, ast.AnnAssign)) and not in_function:
                targets = child.targets if isinstance(child, ast.Assign) else [child.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        name = f"{prefix}.{target.id}" if prefix else target.id
                        out.append(Symbol(name, child.lineno, getattr(child, "end_lineno", child.lineno), "variable"))
            elif starlark and isinstance(child, ast.Expr) and isinstance(child.value, ast.Call) and not prefix:
                for kw in child.value.keywords:
                    if kw.arg == "name" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                        out.append(Symbol(kw.value.value, child.lineno, getattr(child, "end_lineno", child.lineno), "target"))
            elif isinstance(child, (ast.If, ast.Try, ast.With, ast.For, ast.While)) and not in_function:
                visit(child, prefix, in_function)
            elif hasattr(ast, "TryStar") and isinstance(child, getattr(ast, "TryStar")):
                visit(child, prefix, in_function)
    visit(tree, "", False)
    return out


# --- Markdown, YAML, TOML ----------------------------------------------------------------

_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


def _markdown(lines: List[str]) -> List[Symbol]:
    heads: List[Tuple[int, int, str]] = []
    fence = False
    for number, line in enumerate(lines, 1):
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
        match = None if fence else _MD_HEADING.match(line)
        if match:
            heads.append((number, len(match.group(1)), match.group(2).strip()))
    out = []
    for i, (number, level, title) in enumerate(heads):
        end = len(lines)
        for later_number, later_level, _ in heads[i + 1:]:
            if later_level <= level:
                end = later_number - 1
                break
        out.append(Symbol(title, number, end, "section"))
    return out


def _top_level_keys(lines: List[str], pattern: "re.Pattern[str]", kind: str) -> List[Symbol]:
    starts = []
    for number, line in enumerate(lines, 1):
        match = pattern.match(line)
        if match:
            starts.append((number, match.group(1)))
    out = []
    for i, (number, name) in enumerate(starts):
        end = starts[i + 1][0] - 1 if i + 1 < len(starts) else len(lines)
        while end > number and not lines[end - 1].strip():
            end -= 1
        out.append(Symbol(name, number, end, kind))
    return out


# --- brace languages ----------------------------------------------------------------------

_KEYWORDS = {
    "if", "for", "while", "switch", "catch", "return", "else", "do", "new", "throw", "case", "sizeof",
    "function", "typeof", "await", "yield", "delete", "in", "of", "using", "lock", "foreach", "when",
    "elif", "synchronized", "with", "defer", "go", "select", "match", "loop", "unsafe",
}


def _strip_code(line: str, state: Dict[str, bool]) -> str:
    """Remove string contents and comments so braces can be counted."""
    out = []
    i = 0
    n = len(line)
    while i < n:
        if state.get("block"):
            end = line.find("*/", i)
            if end < 0:
                return "".join(out)
            state["block"] = False
            i = end + 2
            continue
        ch = line[i]
        if ch == "/" and i + 1 < n and line[i + 1] == "/":
            break
        if ch == "#" and state.get("hash_comments"):
            break
        if ch == "/" and i + 1 < n and line[i + 1] == "*":
            state["block"] = True
            i += 2
            continue
        if ch in "\"'`":
            quote = ch
            i += 1
            while i < n and line[i] != quote:
                i += 2 if line[i] == "\\" else 1
            i += 1
            out.append('""')
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _block_end(lines: List[str], start: int, hash_comments: bool = False) -> int:
    """1-based end line of the brace block opening at or just after ``start``."""
    state = {"block": False, "hash_comments": hash_comments}
    depth = 0
    opened = False
    for number in range(start, min(len(lines), start + 2000) + 1):
        code = _strip_code(lines[number - 1], state)
        if not opened and number > start + 3:
            return start
        if not opened and ";" in code and "{" not in code:
            return number  # a declaration without a body
        for ch in code:
            if ch == "{":
                depth += 1
                opened = True
            elif ch == "}":
                depth -= 1
                if opened and depth <= 0:
                    return number
    return start if not opened else len(lines)


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _indent_end(lines: List[str], start: int) -> int:
    base = _indent(lines[start - 1])
    end = start
    for number in range(start + 1, len(lines) + 1):
        line = lines[number - 1]
        if not line.strip():
            continue
        if _indent(line) <= base:
            break
        end = number
    return end


def _ruby_end(lines: List[str], start: int) -> int:
    base = _indent(lines[start - 1])
    for number in range(start + 1, len(lines) + 1):
        line = lines[number - 1]
        if line.strip() == "end" and _indent(line) == base:
            return number
    return _indent_end(lines, start)


Rule = Tuple["re.Pattern[str]", str, Callable[["re.Match[str]"], str]]


def _scan(lines: List[str], rules: List[Rule], end_finder, container_kinds=("class", "type", "impl", "namespace")) -> List[Symbol]:
    found: List[Symbol] = []
    for number, line in enumerate(lines, 1):
        for pattern, kind, namer in rules:
            match = pattern.match(line)
            if not match:
                continue
            name = namer(match)
            if not name or name in _KEYWORDS:
                continue
            end = end_finder(lines, number)
            found.append(Symbol(name, number, end, kind))
            break
    # Qualify members with their enclosing containers (class, impl, namespace),
    # outermost first, so nested owners are already qualified.
    found.sort(key=lambda s: (s.start, -s.end))
    qualified_names: Dict[int, str] = {}
    result = []
    for i, sym in enumerate(found):
        owners = [j for j in range(i) if found[j].kind in container_kinds
                  and found[j].start < sym.start and sym.end <= found[j].end]
        if owners and "." not in sym.name:
            owner = max(owners, key=lambda j: found[j].start)
            qualified = f"{qualified_names[owner]}.{sym.name}"
        else:
            qualified = sym.name
        qualified_names[i] = qualified
        result.append(Symbol(qualified, sym.start, sym.end, sym.kind))
    return [s for s in result if s.kind != "impl"] + [s for s in result if s.kind == "impl"]


def _r(pattern: str) -> "re.Pattern[str]":
    return re.compile(pattern)


_GO = [
    (_r(r"^func\s+\(\s*\w*\s*\*?\s*(\w+)(?:\[[^\]]*\])?\s*\)\s*(\w+)"), "method", lambda m: f"{m.group(1)}.{m.group(2)}"),
    (_r(r"^func\s+(\w+)"), "function", lambda m: m.group(1)),
    (_r(r"^type\s+(\w+)\s+(?:struct|interface)\b"), "type", lambda m: m.group(1)),
]
_RUST = [
    (_r(r"^\s*impl(?:<[^>]*>)?\s+(?:[\w:<>,&' ]+?\s+for\s+)?&?([\w:]+)"), "impl", lambda m: m.group(1).split("::")[-1]),
    (_r(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:const\s+)?(?:async\s+)?(?:unsafe\s+)?(?:extern\s+\"\w+\"\s+)?fn\s+(\w+)"), "function", lambda m: m.group(1)),
    (_r(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait|union)\s+(\w+)"), "type", lambda m: m.group(1)),
    (_r(r"^\s*(?:pub(?:\([^)]*\))?\s+)?mod\s+(\w+)\s*\{"), "namespace", lambda m: m.group(1)),
]
_JS = [
    (_r(r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)"), "class", lambda m: m.group(1)),
    (_r(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)"), "function", lambda m: m.group(1)),
    (_r(r"^\s*(?:export\s+)?(?:declare\s+)?(?:interface|enum)\s+([A-Za-z_$][\w$]*)"), "type", lambda m: m.group(1)),
    (_r(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(?:async\s+)?(?:function\b|\([^)]*\)\s*(?::[^=>]+)?=>|[A-Za-z_$][\w$]*\s*=>)"), "function", lambda m: m.group(1)),
    (_r(r"^\s+(?:(?:public|private|protected|static|async|readonly|override|abstract|get|set)\s+)*\*?([A-Za-z_$][\w$]*)\s*(?:<[^>]*>)?\s*\([^)]*\)?\s*(?::\s*[^{=;]+)?\{\s*$"), "method", lambda m: m.group(1)),
]
_CFAMILY = [
    (_r(r"^\s*(?:[\w\[\]@<>.,]+\s+)*(?:class|struct|interface|enum|record|object|trait|protocol|extension)\s+([A-Za-z_][\w]*)"), "class", lambda m: m.group(1)),
    (_r(r"^\s*namespace\s+([\w.:]+)"), "namespace", lambda m: m.group(1)),
    (_r(r"^\s*(?:[\w@<>\[\]]+\s+)*(?:fun|func|def|function)\s+(?:<[^>]*>\s*)?(?:[\w.]+\.)?([A-Za-z_][\w]*)"), "function", lambda m: m.group(1)),
    (_r(r"^\s*(?:[\w:<>,\[\]*&~]+\s+[*&]*)+([A-Za-z_~][\w]*(?:::[A-Za-z_~][\w]*)*)\s*\([^;{]*\)?\s*(?:const\s*)?(?:noexcept\s*)?(?:override\s*)?(?:throws\s+[\w., ]+)?\s*(?:->\s*[\w:<>*& ]+)?\s*\{?\s*$"), "function", lambda m: m.group(1).replace("::", ".")),
]
_SHELL = [
    (_r(r"^\s*(?:function\s+)?([A-Za-z_][\w-]*)\s*\(\)\s*\{?"), "function", lambda m: m.group(1)),
    (_r(r"^\s*function\s+([A-Za-z_][\w-]*)\s*\{?"), "function", lambda m: m.group(1)),
]
_RUBY = [
    (_r(r"^\s*(?:class|module)\s+([\w:]+)"), "class", lambda m: m.group(1).split("::")[-1]),
    (_r(r"^\s*def\s+(?:self\.)?([\w?!=]+)"), "method", lambda m: m.group(1)),
]
_YAML_KEY = re.compile(r"^([A-Za-z_][\w.-]*)\s*:")
_TOML_TABLE = re.compile(r"^\[\[?\s*([^\]]+?)\s*\]\]?\s*$")

_EXT = {
    ".go": "go", ".rs": "rust",
    ".js": "js", ".jsx": "js", ".ts": "js", ".tsx": "js", ".mjs": "js", ".cjs": "js", ".vue": "js", ".svelte": "js",
    ".java": "c", ".kt": "c", ".kts": "c", ".scala": "c", ".cs": "c", ".swift": "c", ".dart": "c",
    ".c": "c", ".h": "c", ".cc": "c", ".cpp": "c", ".cxx": "c", ".hpp": "c", ".hh": "c", ".hxx": "c",
    ".m": "c", ".mm": "c", ".php": "c", ".groovy": "c", ".proto": "c", ".cu": "c",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell",
    ".rb": "ruby",
    ".py": "python", ".pyi": "python",
    ".bzl": "starlark", ".star": "starlark", ".bazel": "starlark",
    ".md": "markdown", ".markdown": "markdown",
    ".yaml": "yaml", ".yml": "yaml",
    ".toml": "toml",
}
_NAMES = {"BUILD": "starlark", "WORKSPACE": "starlark", "Tiltfile": "starlark", "BUCK": "starlark",
          "Makefile": "make", "makefile": "make", "GNUmakefile": "make", "Dockerfile": "none"}
_MAKE_TARGET = re.compile(r"^([A-Za-z0-9_.%/-][^:=#\s]*)\s*:(?!=)")


def language_of(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    if name in _NAMES:
        return _NAMES[name]
    if name.startswith("BUILD.") or name.startswith("WORKSPACE."):
        return "starlark"
    dot = name.rfind(".")
    return _EXT.get(name[dot:].lower(), "generic") if dot >= 0 else "generic"


def index(path: str, text: str) -> List[Symbol]:
    """Every symbol in a file, with qualified names where nesting is known."""
    lang = language_of(path)
    lines = text.splitlines()
    try:
        if lang == "python":
            return _python(text)
        if lang == "starlark":
            return _python(text, starlark=True)
    except (SyntaxError, ValueError, RecursionError):
        return _scan(lines, [(_r(r"^\s*(?:async\s+)?def\s+(\w+)"), "function", lambda m: m.group(1)),
                             (_r(r"^\s*class\s+(\w+)"), "class", lambda m: m.group(1))], _indent_end)
    if lang == "markdown":
        return _markdown(lines)
    if lang == "yaml":
        return _top_level_keys(lines, _YAML_KEY, "key")
    if lang == "toml":
        return _top_level_keys(lines, _TOML_TABLE, "table")
    if lang == "make":
        return _top_level_keys(lines, _MAKE_TARGET, "target")
    if lang == "go":
        return _scan(lines, _GO, _block_end)
    if lang == "rust":
        return _scan(lines, _RUST, _block_end)
    if lang == "js":
        return _scan(lines, _JS, _block_end)
    if lang == "c":
        return _scan(lines, _CFAMILY, _block_end)
    if lang == "shell":
        return _scan(lines, _SHELL, lambda ls, n: _block_end(ls, n, hash_comments=True))
    if lang == "ruby":
        return _scan(lines, _RUBY, _ruby_end)
    if lang == "none":
        return []
    return _scan(lines, _CFAMILY + _JS[:2], _block_end)


def normalize_query(symbol: str) -> str:
    text = symbol.strip()
    text = re.sub(r"\(.*\)$", "", text)
    text = text.replace("::", ".").replace("#", ".").replace("->", ".")
    return text.strip(". ")


def find(symbols: List[Symbol], query: str) -> Optional[Symbol]:
    """Resolve an anchor's symbol: exact name, then unique suffix, then unique last name."""
    wanted = normalize_query(query)
    if not wanted:
        return None
    for sym in symbols:
        if sym.name == wanted:
            return sym
    lowered = wanted.lower()
    for sym in symbols:
        if sym.name.lower() == lowered:
            return sym
    suffix = [s for s in symbols if s.name.endswith("." + wanted)]
    if suffix:
        return min(suffix, key=lambda s: (s.name.count("."), s.start))
    last = wanted.split(".")[-1]
    by_last = [s for s in symbols if s.name.split(".")[-1] == last]
    if len(by_last) == 1:
        return by_last[0]
    owner = wanted.split(".")[0] if "." in wanted else None
    if owner and len(by_last) > 1:
        owned = [s for s in by_last if s.name.startswith(owner + ".")]
        if len(owned) == 1:
            return owned[0]
    return None


def enclosing(symbols: List[Symbol], line: int) -> Optional[Symbol]:
    """The innermost symbol containing ``line``."""
    best = None
    for sym in symbols:
        if sym.start <= line <= sym.end and sym.kind not in ("variable",):
            if best is None or (sym.end - sym.start) <= (best.end - best.start):
                best = sym
    return best

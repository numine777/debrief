"""Test fixtures: an isolated home, archive and git identity per test."""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

ISOLATED_VARS = [
    "HOME", "AI_SESSIONS_DIR", "DEBRIEF_CONFIG_DIR", "DEBRIEF_HOME", "DEBRIEF_BIN_DIR",
    "XDG_DATA_HOME", "XDG_CONFIG_HOME", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM",
    "DEBRIEF_HOSTNAME", "DEBRIEF_HARNESS", "CODEX_HOME", "PI_CODING_AGENT_DIR", "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT", "PI_CODING_AGENT", "DEBRIEF_PYZ",
]


def git(cwd, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc.stdout.strip()


class IsolatedTestCase(unittest.TestCase):
    """Runs each test with its own HOME, archive, config and git identity."""

    def setUp(self) -> None:
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp(prefix="debrief-test-")).resolve()
        self._saved_env = {k: os.environ.get(k) for k in ISOLATED_VARS}
        self._saved_cwd = os.getcwd()
        for key in ISOLATED_VARS:
            os.environ.pop(key, None)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.archive = self.tmp / "archive"
        gitconfig = self.tmp / "gitconfig"
        gitconfig.write_text(
            "[user]\n\tname = Test Dev\n\temail = dev@example.com\n"
            "[commit]\n\tgpgsign = false\n[tag]\n\tgpgsign = false\n"
            "[init]\n\tdefaultBranch = main\n"
        )
        os.environ.update({
            "HOME": str(self.home),
            "AI_SESSIONS_DIR": str(self.archive),
            "DEBRIEF_CONFIG_DIR": str(self.tmp / "config"),
            "DEBRIEF_HOME": str(self.tmp / "tool"),
            "DEBRIEF_BIN_DIR": str(self.tmp / "bin"),
            "GIT_CONFIG_GLOBAL": str(gitconfig),
            "GIT_CONFIG_NOSYSTEM": "1",
            "DEBRIEF_HOSTNAME": "testhost",
            "DEBRIEF_HARNESS": "test-harness",
        })

    def tearDown(self) -> None:
        os.chdir(self._saved_cwd)
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)
        super().tearDown()

    # --- repos ---------------------------------------------------------------

    def make_repo(self, name: str = "repo", files: Optional[dict] = None, remote: Optional[str] = None) -> Path:
        repo = self.tmp / name
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        for rel, text in (files or {"app.py": "def main():\n    return 0\n"}).items():
            self.write(repo / rel, text)
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "Initial commit")
        if remote:
            git(repo, "remote", "add", "origin", remote)
        return repo

    def write(self, path: Path, text: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def commit(self, repo: Path, message: str, files: Optional[dict] = None) -> str:
        for rel, text in (files or {}).items():
            if text is None:
                (repo / rel).unlink()
            else:
                self.write(repo / rel, text)
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", message)
        return git(repo, "rev-parse", "HEAD")

    # --- running commands --------------------------------------------------------

    def run_session(self, repo: Path, *args: str) -> str:
        from debrief import session

        os.chdir(repo)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = session.main(list(args))
        self.last_code = code
        return out.getvalue()

    def run_cli(self, *args: str, cwd: Optional[Path] = None) -> str:
        from debrief import cli

        if cwd:
            os.chdir(cwd)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(list(args))
        self.last_code = code
        return out.getvalue()

    def init_project(self, repo: Path) -> str:
        from debrief import projects

        return projects.init_project(repo).project_id

    def feature_dir(self, project_id: str, feature_id: str) -> Path:
        return self.archive / "projects" / project_id / "features" / feature_id

    def journal(self, feature_dir: Path) -> Path:
        """The open session's journal, or the most recently started one."""
        import json

        state = self.archive / ".state" / "current.json"
        if state.exists():
            for entry in json.loads(state.read_text())["sessions"].values():
                if entry["feature_id"] == feature_dir.name:
                    return feature_dir / "sessions" / entry["session_id"] / "journal.md"
        sessions = sorted((feature_dir / "sessions").iterdir(), key=lambda p: p.stat().st_mtime_ns)
        return sessions[-1] / "journal.md"

    def append_journal(self, feature_dir: Path, kind: str, text: str, stamp: str = "2026-10-02T19:41Z") -> None:
        path = self.journal(feature_dir)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"### {stamp} · {kind}\n{text}\n\n")

    def write_records(self, feature_dir: Path, systems: Optional[List[str]] = None, brief_extra: str = "",
                      tests_yaml: Optional[str] = None) -> None:
        """Minimal valid closeout records anchored at app.py."""
        self.write(feature_dir / "brief.md", (
            "---\nfeature_id: " + feature_dir.name + "\ntitle: Test feature\nstatus: ready_for_review\n"
            "sessions: []\nreview_first: []\nincidental: []\n" + brief_extra + "---\n\n"
            "## Intent\nTest.\n\n## What was built\nThings.\n\n## Divergences\nNone.\n\n"
            "## Risks and gaps\nNone.\n\n## Follow-ups\nNone.\n"))
        for sid in systems or ["app"]:
            self.write(feature_dir / "systems" / f"{sid}.md", (
                f"---\nid: {sid}\ntitle: App\nchange: new\ndepends_on: []\n"
                "anchors:\n  - {path: app.py, symbol: main, role: core}\ncritical_paths: []\ndecisions: []\n---\n\n"
                "## Purpose\nP.\n\n## Change\nNew.\n\n## How it works\nH.\n\n## Limitations\nL.\n"))
        self.write(feature_dir / "tests.yaml", tests_yaml or "tests: []\ngaps: []\n")

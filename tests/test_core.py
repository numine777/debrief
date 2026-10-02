"""Residency guards, project identity, archive sync and the zipapp."""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from tests.helpers import ROOT, IsolatedTestCase, git

from debrief import archive, buildzip, config, projects, residency, util


class ResidencyTests(IsolatedTestCase):
    def test_host_of(self):
        cases = {
            "git@git.corp.example:team/repo.git": "git.corp.example",
            "ssh://git@hub.corp.example:2222/x.git": "hub.corp.example",
            "https://models.corp.example/v1": "models.corp.example",
            "/srv/archives/x.git": None,
            "file:///srv/x.git": None,
            "../relative/x.git": None,
            "C:\\archives\\x": None,
        }
        for url, host in cases.items():
            self.assertEqual(residency.host_of(url), host, url)

    def test_allowlist(self):
        self.write(self.tmp / "config" / "config", "[residency]\nallow_hosts = git.corp.example, *.intranet.example\n")
        cfg = config.load()
        residency.check_url("git@git.corp.example:a/b.git", "test", cfg)
        residency.check_url("https://models.eu.intranet.example/v1", "test", cfg)
        residency.check_url("/local/path", "test", cfg)
        residency.check_url("http://localhost:8080", "test", cfg)
        with self.assertRaises(residency.ResidencyError) as ctx:
            residency.check_url("https://api.example.com/v1", "Compaction endpoint", cfg)
        self.assertIn("api.example.com is not on the residency allowlist", str(ctx.exception))

    def test_cloud_synced_archive_is_refused(self):
        for path in ["/Users/me/Library/Mobile Documents/com~apple~CloudDocs/a",
                     "/Users/me/Library/CloudStorage/OneDrive-Corp/a", "/home/me/Dropbox/a",
                     "/home/me/OneDrive - Corp/a"]:
            with self.assertRaises(residency.ResidencyError, msg=path):
                residency.check_archive_path(Path(path))
        residency.check_archive_path(self.tmp / "archive")

    def test_init_refuses_cloud_archive(self):
        repo = self.make_repo()
        os.environ["AI_SESSIONS_DIR"] = str(self.tmp / "Dropbox" / "ai-sessions")
        with self.assertRaises(residency.ResidencyError):
            projects.init_project(repo)

    def test_archive_remote_must_be_allowed(self):
        repo = self.make_repo()
        with self.assertRaises(residency.ResidencyError):
            projects.init_project(repo, remote="git@github.com:me/archive.git")


class ProjectTests(IsolatedTestCase):
    def test_normalize_remote(self):
        same = ["git@github.com:Owner/Repo.git", "https://github.com/owner/repo", "ssh://git@github.com/owner/repo.git",
                "https://user@github.com/owner/repo/"]
        self.assertEqual({projects.normalize_remote(u) for u in same}, {"github.com/owner/repo"})
        self.assertEqual(projects.normalize_remote("ssh://git@git.corp:7999/t/r.git"), "git.corp/t/r")

    def test_init_writes_nothing_into_the_repo(self):
        repo = self.make_repo(remote="git@git.corp.example:team/svc.git")
        before = sorted(p.name for p in repo.iterdir())
        result = projects.init_project(repo)
        self.assertEqual(result.project_id, "git.corp.example-team-svc")
        self.assertEqual(result.name, "svc")
        self.assertEqual(sorted(p.name for p in repo.iterdir()), before)
        self.assertEqual(git(repo, "status", "--porcelain", "--ignored"), "")
        self.assertEqual(git(repo, "for-each-ref", "--format=%(refname)"), "refs/heads/main")
        meta = json.loads((result.project_dir / "project.json").read_text())
        self.assertEqual(meta["remote_url"], "git@git.corp.example:team/svc.git")
        self.assertIn("Register svc", git(result.project_dir, "log", "--format=%s"))

    def test_bare_repo_with_worktrees_registers_once(self):
        origin = self.make_repo("origin")
        bare = self.tmp / "bare.git"
        git(self.tmp, "clone", "-q", "--bare", str(origin), str(bare))
        wt1, wt2 = self.tmp / "wt1", self.tmp / "wt2"
        git(bare, "worktree", "add", "-q", "-b", "a", str(wt1))
        git(bare, "worktree", "add", "-q", "-b", "b", str(wt2))
        first = projects.init_project(wt1)
        second = projects.init_project(wt2)
        self.assertEqual(first.project_id, second.project_id)
        self.assertFalse(second.created)
        self.assertEqual(projects.find_project(wt2)[0], first.project_id)
        entry = projects.registered_projects()[first.project_id]
        self.assertEqual(len(entry["common_dirs"]), 1)

    def test_repo_without_remote_gets_path_id(self):
        repo = self.make_repo("my-service")
        pid = projects.init_project(repo).project_id
        self.assertTrue(pid.startswith("local-my-service-"))


class ArchiveSyncTests(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.remote = self.tmp / "remote.git"
        git(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.remote))
        self.a = self.tmp / "a" / "proj"
        self.b = self.tmp / "b" / "proj"
        archive.ensure_repo(self.a)
        archive.set_remote(self.a, str(self.remote))
        util.write_json(self.a / "project.json", {"project_id": "proj"})
        archive.sync(self.a, "Seed")
        git(self.tmp, "clone", "-q", str(self.remote), str(self.b))

    def test_comments_merge_by_id(self):
        path = "features/f/comments.json"
        util.write_json(self.a / path, {"comments": [{"id": "c1", "body": "a", "created_at": "1", "updated_at": "1"}]})
        archive.sync(self.a, "A")
        util.write_json(self.b / path, {"comments": [{"id": "c2", "body": "b", "created_at": "2", "updated_at": "2"}]})
        result = archive.sync(self.b, "B")
        self.assertTrue(result["pushed"], result)
        merged = json.loads((self.b / path).read_text())
        self.assertEqual([c["id"] for c in merged["comments"]], ["c1", "c2"])
        archive.sync(self.a, "A again")
        self.assertEqual(len(json.loads((self.a / path).read_text())["comments"]), 2)

    def test_leg_files_merge_close_requests(self):
        path = "features/f/legs/leg-01.json"
        base = {"leg_id": "leg-01", "commits": [{"sha": "1"}], "close_requested_at": None, "closed_at": None}
        util.write_json(self.a / path, base)
        archive.sync(self.a, "A")
        archive.pull(self.b)
        util.write_json(self.a / path, dict(base, commits=[{"sha": "1"}, {"sha": "2"}]))
        archive.sync(self.a, "A2")
        util.write_json(self.b / path, dict(base, close_requested_at="2026-10-02T20:00:00Z"))
        archive.sync(self.b, "B")
        merged = json.loads((self.b / path).read_text())
        self.assertEqual([c["sha"] for c in merged["commits"]], ["1", "2"])
        self.assertEqual(merged["close_requested_at"], "2026-10-02T20:00:00Z")

    def test_other_conflicts_keep_both_versions(self):
        path = self.a / "features" / "f" / "brief.md"
        self.write(path, "base\n")
        archive.sync(self.a, "A")
        archive.pull(self.b)
        self.write(path, "from a\n")
        archive.sync(self.a, "A2")
        self.write(self.b / "features" / "f" / "brief.md", "from b\n")
        result = archive.sync(self.b, "B")
        self.assertEqual(result["conflicts"], ["features/f/brief.md"])
        self.assertEqual((self.b / "features" / "f" / "brief.md").read_text(), "from b\n")
        copies = list((self.b / "features" / "f").glob("brief.md.conflict-*"))
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].read_text(), "from a\n")
        self.assertIn("features/f/brief.md", (self.b / "conflicts.json").read_text())

    def test_pull_into_empty_archive(self):
        empty = self.tmp / "c" / "proj"
        archive.ensure_repo(empty)
        archive.set_remote(empty, str(self.remote))
        result = archive.pull(empty)
        self.assertTrue(result["pulled"])
        self.assertTrue((empty / "project.json").exists())


class ZipappTests(IsolatedTestCase):
    def test_build_is_reproducible_and_runs(self):
        first = buildzip.build(self.tmp / "one.pyz", ROOT / "src" / "debrief")
        second = buildzip.build(self.tmp / "two.pyz", ROOT / "src" / "debrief")
        self.assertEqual(first.read_bytes(), second.read_bytes())
        out = subprocess.run([sys.executable, str(first), "--version"], stdout=subprocess.PIPE, text=True)
        self.assertTrue(out.stdout.startswith("debrief "))
        repo = self.make_repo()
        out = subprocess.run([sys.executable, str(first), "init", str(repo)], stdout=subprocess.PIPE, text=True)
        self.assertIn("Registered", out.stdout)
        out = subprocess.run([sys.executable, str(first), "session", "start"], cwd=str(repo),
                             stdout=subprocess.PIPE, text=True)
        self.assertIn("leg-01 (new)", out.stdout)
        self.assertIn("ai-session/SKILL.md", out.stdout)


if __name__ == "__main__":
    unittest.main()

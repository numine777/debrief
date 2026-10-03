import json
import unittest

from tests.helpers import IsolatedTestCase, git

from debrief import records, session, util


class SessionFixture(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.repo = self.make_repo()
        git(self.repo, "checkout", "-q", "-b", "feat/retry")
        self.pid = self.init_project(self.repo)
        self.fdir = self.feature_dir(self.pid, "feat--retry")

    def legs(self):
        return records.load_legs(self.fdir)


class SessionFlowTests(SessionFixture):
    def test_untracked_repo_is_skipped(self):
        other = self.make_repo("other")
        out = self.run_session(other, "start")
        self.assertIn("isn't tracked", out)
        self.assertEqual(self.last_code, 0)

    def test_outside_git(self):
        out = self.run_session(self.tmp, "start")
        self.assertIn("Not inside a git repository", out)

    def test_start_opens_leg_and_session(self):
        base = git(self.repo, "rev-parse", "main")
        out = self.run_session(self.repo, "start", "--task", "Add retries")
        self.assertIn("leg-01 (new)", out)
        self.assertIn(str(self.fdir), out)
        legs = self.legs()
        self.assertEqual(len(legs), 1)
        self.assertEqual(legs[0]["base_ref"], base)
        self.assertEqual(legs[0]["branch"], "feat/retry")
        sessions = list((self.fdir / "sessions").iterdir())
        self.assertEqual(len(sessions), 1)
        meta = json.loads((sessions[0] / "session.json").read_text())
        self.assertEqual(meta["status"], "in_progress")
        self.assertEqual(meta["harness"], "test-harness")
        self.assertEqual(meta["task"], "Add retries")
        self.assertEqual(meta["protocol"], records.PROTOCOL)
        # Nothing was written into the repository.
        self.assertEqual(git(self.repo, "status", "--porcelain", "--ignored"), "")

    def test_now_logs_commits_with_patch_ids(self):
        self.run_session(self.repo, "start")
        sha = self.commit(self.repo, "Add retry", {"retry.py": "def retry():\n    pass\n"})
        out = self.run_session(self.repo, "now")
        first = out.splitlines()[0]
        self.assertIsNotNone(util.parse_iso(first))
        self.assertIn("Logged 1 commit", out)
        commits = self.legs()[0]["commits"]
        self.assertEqual(commits[0]["sha"], sha)
        self.assertTrue(commits[0]["patch_id"])
        self.run_session(self.repo, "now")
        self.assertEqual(len(self.legs()[0]["commits"]), 1)

    def test_commits_between_sessions_are_not_logged(self):
        self.run_session(self.repo, "start")
        self.run_session(self.repo, "close")
        self.commit(self.repo, "Developer change", {"dev.py": "x = 1\n"})
        self.run_session(self.repo, "start")
        self.commit(self.repo, "Agent change", {"agent.py": "y = 2\n"})
        self.run_session(self.repo, "now")
        subjects = [c["subject"] for c in self.legs()[0]["commits"]]
        self.assertEqual(subjects, ["Agent change"])

    def test_unclosed_session_keeps_its_commits(self):
        self.run_session(self.repo, "start")
        first = self.legs()[0]["sessions"][0]["session_id"]
        self.commit(self.repo, "Made before the agent stopped", {"a.py": "a = 1\n"})
        out = self.run_session(self.repo, "start")
        self.assertIn("ended without `close`", out)
        commits = self.legs()[0]["commits"]
        self.assertEqual(commits[0]["session_id"], first)

    def test_run_records_exit_code_and_output(self):
        self.run_session(self.repo, "start")
        self.run_session(self.repo, "run", "echo hello; exit 3")
        self.assertEqual(self.last_code, 3)
        runs = list(self.fdir.glob("sessions/*/runs/*.json"))
        self.assertEqual(len(runs), 1)
        record = json.loads(runs[0].read_text())
        self.assertEqual(record["exit_code"], 3)
        self.assertEqual(record["command"], "echo hello; exit 3")
        self.assertIn("hello", record["output_tail"])
        self.assertEqual(record["head"], git(self.repo, "rev-parse", "HEAD"))

    def test_run_without_session_still_runs(self):
        self.run_session(self.repo, "run", "true")
        self.assertEqual(self.last_code, 0)
        self.assertFalse(list(self.archive.glob("**/runs/*.json")))

    def test_changed_reports_explanations(self):
        self.run_session(self.repo, "start")
        self.commit(self.repo, "Change app", {"app.py": "def main():\n    return 1\n", "notes.txt": "n\n"})
        self.write(self.repo / "wip.py", "pending = True\n")
        self.write_records(self.fdir)
        out = self.run_session(self.repo, "changed")
        self.assertIn("app.py", out)
        self.assertIn("explained by app", out)
        self.assertIn("notes.txt  NOT EXPLAINED", out)
        self.assertIn("Uncommitted", out)
        self.assertIn("wip.py", out)

    def test_close_request_and_feedback_are_relayed(self):
        self.run_session(self.repo, "start")
        leg = self.legs()[0]
        leg["close_requested_at"] = util.now_iso()
        leg["close_requested_by"] = "dev"
        util.write_json(self.fdir / "legs" / "leg-01.json", leg)
        self.write(self.fdir / "feedback.md", "## 2026-10-02T20:00:00Z · dev\n\n1. app.py:1\n   Fix it.\n")
        out = self.run_session(self.repo, "now")
        self.assertIn("REQUEST from dev: close out leg-01", out)
        self.assertIn("FEEDBACK: 1 new review item", out)
        out = self.run_session(self.repo, "now")
        self.assertIn("REQUEST", out)
        self.assertNotIn("FEEDBACK", out)

    def test_close_requires_handoff_warning(self):
        self.run_session(self.repo, "start")
        out = self.run_session(self.repo, "close")
        self.assertIn("no `handoff` entry", out)
        self.run_session(self.repo, "start")
        self.append_journal(self.fdir, "handoff", "Done.")
        out = self.run_session(self.repo, "close", "blocked")
        self.assertNotIn("handoff", out.split("closed")[1])
        metas = [json.loads(p.read_text()) for p in self.fdir.glob("sessions/*/session.json")]
        self.assertIn("blocked", {m["status"] for m in metas})

    def test_publish_refuses_without_records_then_closes_leg(self):
        self.run_session(self.repo, "start")
        sha = self.commit(self.repo, "Change app", {"app.py": "def main():\n    return 2\n"})
        out = self.run_session(self.repo, "publish")
        self.assertEqual(self.last_code, 1)
        self.assertIn("Refusing to publish", out)
        self.write_records(self.fdir)
        self.append_journal(self.fdir, "handoff", "Done.")
        out = self.run_session(self.repo, "publish")
        self.assertEqual(self.last_code, 0, out)
        leg = self.legs()[0]
        self.assertTrue(leg["closed_at"])
        self.assertEqual(leg["head_ref"], sha)
        self.assertEqual(leg["head_tree"], git(self.repo, "rev-parse", "HEAD^{tree}"))
        project_dir = self.archive / "projects" / self.pid
        log = git(project_dir, "log", "--format=%s")
        self.assertIn("Close leg-01 of feat--retry: Test feature", log)
        self.assertEqual(git(project_dir, "status", "--porcelain"), "")
        # The next session opens leg-02 based on leg-01's head.
        out = self.run_session(self.repo, "start")
        self.assertIn("leg-02 (new)", out)
        self.assertIn("Existing brief", out)
        self.assertEqual(self.legs()[1]["base_ref"], sha)

    def test_publish_refuses_dirty_worktree(self):
        self.run_session(self.repo, "start")
        self.write_records(self.fdir)
        self.write(self.repo / "app.py", "changed = True\n")
        out = self.run_session(self.repo, "publish")
        self.assertEqual(self.last_code, 1)
        self.assertIn("commit the remaining code first", out)

    def test_context_does_not_change_state(self):
        out = self.run_session(self.repo, "context")
        self.assertIn("debrief-session start`", out)
        self.assertFalse(self.fdir.exists())
        self.run_session(self.repo, "start")
        out = self.run_session(self.repo, "context")
        self.assertIn("is open for feature feat--retry", out)

    def test_worktrees_share_one_project(self):
        wt = self.tmp / "wt"
        git(self.repo, "worktree", "add", "-q", "-b", "feat/other", str(wt))
        out = self.run_session(wt, "start")
        self.assertIn("feat--other", out)
        self.assertTrue(self.feature_dir(self.pid, "feat--other").exists())

    def test_detect_harness_env(self):
        import os

        os.environ.pop("DEBRIEF_HARNESS", None)
        os.environ["CLAUDECODE"] = "1"
        self.assertEqual(session.detect_harness(), "claude-code")


class ProtocolFixTests(SessionFixture):
    """The protocol issues the independent QA pass found."""

    def test_now_prints_only_the_stamp_on_stdout(self):
        self.run_session(self.repo, "start")
        self.write(self.fdir / "feedback.md", "## 2026-10-02T20:00:00Z · dev\n\n1. app.py:1\n   Fix it.\n")
        self.commit(self.repo, "Agent change\n\nBody.", {"app.py": "x = 1\n"})
        out = self.run_session(self.repo, "now")
        self.assertEqual(self.last_stdout.strip().count("\n"), 0)  # TS=$(bin/session now) gets just the time
        self.assertIsNotNone(util.parse_iso(self.last_stdout.strip()))
        self.assertIn("FEEDBACK: 1 new review item", out)
        self.assertIn("Logged 1 commit", out)

    def test_now_survives_a_reader_that_stops_early(self):
        import os
        import subprocess
        import sys

        from tests.helpers import SRC

        self.run_session(self.repo, "start")
        env = dict(os.environ, PYTHONPATH=str(SRC))
        proc = subprocess.run(f"{sys.executable} -m debrief session now | head -c 1", shell=True, cwd=str(self.repo),
                              env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertNotIn("BrokenPipe", proc.stderr)

    def test_closeout_in_a_later_turn_records_its_work(self):
        # Turn 1: work, hand off, close (the Finish rule).
        self.run_session(self.repo, "start")
        self.commit(self.repo, "Feature work\n\nBody.", {"app.py": "def main():\n    return 2\n"})
        self.append_journal(self.fdir, "handoff", "Done for now.")
        self.run_session(self.repo, "close")
        # Turn 2: the developer asks for closeout; the skill opens its own session first.
        self.run_session(self.repo, "start", "--task", "Close out the leg")
        self.commit(self.repo, "Closeout fix\n\nBody.", {"app.py": "def main():\n    return 3\n"})
        out = self.run_session(self.repo, "run", "true")
        self.assertNotIn("not recorded", out)
        self.write_records(self.fdir)
        self.append_journal(self.fdir, "handoff", "Closed out.")
        out = self.run_session(self.repo, "publish")  # publish closes the session itself
        self.assertEqual(self.last_code, 0, out)
        logged = [c["subject"] for c in self.legs()[0]["commits"]]
        self.assertEqual(logged, ["Feature work", "Closeout fix"])
        statuses = [json.loads(p.read_text())["status"] for p in self.fdir.glob("sessions/*/session.json")]
        self.assertEqual(sorted(statuses), ["complete", "complete"])

    def test_detached_head_keeps_its_session(self):
        git(self.repo, "checkout", "-q", "--detach")
        self.run_session(self.repo, "start")
        self.commit(self.repo, "Detached work\n\nBody.", {"app.py": "y = 1\n"})
        self.assertIn("Logged 1 commit", self.run_session(self.repo, "now"))
        self.run_session(self.repo, "close")
        self.assertEqual(self.last_code, 0)
        features = [p.name for p in (self.archive / "projects" / self.pid / "features").iterdir()]
        self.assertEqual(len([f for f in features if f.startswith("detached-")]), 1, features)

    def test_now_without_a_session_writes_nothing(self):
        out = self.run_session(self.repo, "now")
        self.assertIn("No open Debrief session", out)
        self.assertFalse((self.archive / "projects" / self.pid / "features").exists())

    def test_branches_that_slug_alike_get_separate_features(self):
        git(self.repo, "checkout", "-q", "-b", "feat/a+b")
        self.run_session(self.repo, "start")
        self.run_session(self.repo, "close")
        git(self.repo, "checkout", "-q", "-b", "feat/a@b")
        out = self.run_session(self.repo, "start")
        features = sorted(p.name for p in (self.archive / "projects" / self.pid / "features").iterdir())
        self.assertEqual(len(features), 2, features)
        self.assertIn(f"feature  {features[1]} (branch feat/a@b)", out)

    def test_publish_refuses_untracked_files(self):
        self.run_session(self.repo, "start")
        self.commit(self.repo, "Change\n\nBody.", {"app.py": "def main():\n    return 2\n"})
        self.write_records(self.fdir)
        self.write(self.repo / "extra.py", "while True:\n    pass\n")
        out = self.run_session(self.repo, "publish")
        self.assertEqual(self.last_code, 1)
        self.assertIn("extra.py (untracked", out)

    def test_a_leg_without_code_changes_needs_no_systems(self):
        self.run_session(self.repo, "start", "--task", "Investigate the flaky test")
        self.write_records(self.fdir, systems=[])
        for path in (self.fdir / "systems").glob("*.md"):
            path.unlink()
        self.append_journal(self.fdir, "handoff", "Found the cause; no code changed.")
        out = self.run_session(self.repo, "publish")
        self.assertEqual(self.last_code, 0, out)

    def test_superseded_session_is_closed_out(self):
        self.run_session(self.repo, "start")
        first = self.legs()[0]["sessions"][0]["session_id"]
        self.run_session(self.repo, "start")
        meta = json.loads((self.fdir / "sessions" / first / "session.json").read_text())
        self.assertEqual(meta["status"], "superseded")
        self.assertTrue(meta["ended_at"])
        self.assertFalse([i for i in records.all_issues(records.load_feature(self.fdir)) if "status" in i["message"]])

    def test_start_points_at_the_latest_journal_with_entries(self):
        self.run_session(self.repo, "start")
        self.append_journal(self.fdir, "plan", "Real work.")
        real = self.journal(self.fdir)
        self.run_session(self.repo, "close")
        empty = self.fdir / "sessions" / "29991231T000000Z-ffff"  # another host's session, just started
        self.write(empty / "journal.md", "# Journal\n")
        self.write(empty / "session.json", "{}")
        out = self.run_session(self.repo, "start")
        self.assertIn(str(real), out)

    def test_changed_judges_hunks_and_keeps_status_codes(self):
        base = "def a():\n    return 1\n\n\ndef b():\n    return 2\n"
        git(self.repo, "checkout", "-q", "main")
        self.commit(self.repo, "Base\n\nB.", {"mod.py": base})
        git(self.repo, "checkout", "-q", "feat/retry")
        git(self.repo, "merge", "-q", "--ff-only", "main")
        self.run_session(self.repo, "start")
        self.commit(self.repo, "Change b\n\nB.", {"mod.py": base.replace("return 2", "return 3")})
        self.write_records(self.fdir, systems=[])
        self.write(self.fdir / "systems" / "a.md", "---\nid: a\ntitle: A\nchange: new\nanchors:\n  - {path: mod.py, symbol: a}\n"
                                                   "critical_paths: []\n---\n\n## Purpose\nP.\n")
        self.write(self.repo / "app.py", "changed = True\n")
        out = self.run_session(self.repo, "changed")
        self.assertIn("mod.py  NOT EXPLAINED at line 6", out)  # a's anchor sits next to it, but doesn't explain it
        self.assertIn("   M app.py", out)  # the first status entry keeps its leading space

    def test_detached_work_keeps_its_feature_across_turns(self):
        git(self.repo, "checkout", "-q", "--detach")
        self.run_session(self.repo, "start")
        [fdir] = list((self.archive / "projects" / self.pid / "features").iterdir())
        self.commit(self.repo, "Detached work\n\nBody.", {"app.py": "y = 1\n"})
        self.run_session(self.repo, "now")
        self.append_journal(fdir, "handoff", "Turn one done.")
        self.run_session(self.repo, "close")  # the Finish rule closes every turn
        self.run_session(self.repo, "start", "--task", "Close out the leg")  # a later turn, same HEAD
        features = [p.name for p in (self.archive / "projects" / self.pid / "features").iterdir()]
        self.assertEqual(len(features), 1, features)

    def test_unborn_branch_still_needs_systems_to_publish(self):
        repo = self.tmp / "unborn"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        pid = self.init_project(repo)
        fdir = self.feature_dir(pid, "main")
        self.run_session(repo, "start")
        for i in range(2):
            self.commit(repo, f"Commit {i}\n\nBody.", {f"f{i}.py": f"v = {i}\n"})
        self.write_records(fdir)
        for path in (fdir / "systems").glob("*.md"):
            path.unlink()
        self.append_journal(fdir, "handoff", "Done.")
        out = self.run_session(repo, "publish")
        self.assertEqual(self.last_code, 1, out)
        self.assertIn("no system files", out)
        self.assertIn("(start)..", self.run_session(repo, "changed"))

    def test_a_new_leg_on_the_default_branch_starts_at_head(self):
        git(self.repo, "checkout", "-q", "main")
        fdir = self.feature_dir(self.pid, "main")
        self.run_session(self.repo, "start")
        self.commit(self.repo, "Mine\n\nBody.", {"mine.py": "m = 1\n"})
        self.write_records(fdir)
        self.append_journal(fdir, "handoff", "Done.")
        self.run_session(self.repo, "publish")
        others = self.commit(self.repo, "Someone else's\n\nBody.", {"theirs.py": "t = 1\n"})
        self.run_session(self.repo, "start")
        self.assertEqual(records.load_legs(fdir)[1]["base_ref"], others)

    def test_launcher_under_home_is_written_with_a_tilde(self):
        import os

        from debrief import install

        os.environ["DEBRIEF_BIN_DIR"] = str(self.home / ".local" / "bin")
        self.assertEqual(install.session_launcher_text(), "~/.local/bin/debrief-session")


if __name__ == "__main__":
    unittest.main()

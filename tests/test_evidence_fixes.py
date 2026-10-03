"""Evidence correctness: what the independent QA pass found, as regression tests.

Each test reproduces one reported scenario: evidence lost on hosts without the
feature's commits, coverage counting diff context, commits misattributed
after amend, rebase or merge, and the smaller ingest edge cases.
"""

import json
import shutil
import unittest

from tests.helpers import IsolatedTestCase, git

from debrief import index, ingest, rules, squash

SYSTEM = """---
id: {sid}
title: {sid}
change: new
depends_on: []
anchors:
{anchors}
critical_paths: []
decisions: []
---

## Purpose
P.

## Change
C.

## How it works
H.

## Limitations
L.
"""


class EvidenceFixture(IsolatedTestCase):
    feature = "feat--fix"

    def start(self, files, branch="feat/fix"):
        self.repo = self.make_repo(files=files)
        git(self.repo, "checkout", "-q", "-b", branch)
        self.pid = self.init_project(self.repo)
        self.fdir = self.feature_dir(self.pid, self.feature)
        self.run_session(self.repo, "start")

    def system(self, sid, *anchors):
        lines = "\n".join(f"  - {a}" for a in anchors)
        self.write(self.fdir / "systems" / f"{sid}.md", SYSTEM.format(sid=sid, anchors=lines))

    def records(self, *systems):
        self.write_records(self.fdir, systems=[])
        for sid, anchors in systems:
            self.system(sid, *anchors)

    def evidence(self):
        return ingest.ingest_feature(self.pid, self.feature)

    def hunks(self, ev, path):
        return [h for f in ev["files"] if f["path"] == path for h in f["hunks"]]


class CoverageTests(EvidenceFixture):
    BASE = "def a():\n    return 1\n\ndef b():\n    return 2\n"

    def test_context_lines_do_not_explain_a_neighbouring_change(self):
        self.start({"mod.py": self.BASE})
        self.commit(self.repo, "Change b\n\nTwo now three.", {"mod.py": self.BASE.replace("return 2", "return 3")})
        self.run_session(self.repo, "now")
        self.records(("only-a", ["{path: mod.py, symbol: a}"]))
        ev = self.evidence()
        [hunk] = self.hunks(ev, "mod.py")
        self.assertEqual(hunk["state"], "unclaimed")  # it used to be "covered" by a's context lines
        self.assertEqual(hunk["symbol"], "b")  # labelled by the changed line, not the first context line
        item = [i for i in ev["queue"] if i["kind"] == "unclaimed-hunk"][0]
        self.assertEqual(item["line"], 5)
        commit = json.loads(next((self.fdir / "evidence" / "commits").glob("*.json")).read_text())
        self.assertEqual(commit["files"][0]["hunks"][0]["state"], "unclaimed")

    def test_fused_changes_are_judged_one_by_one(self):
        self.start({"mod.py": self.BASE})
        self.commit(self.repo, "Change both\n\nBoth.", {"mod.py": self.BASE.replace("return 1", "return 10")
                                                       .replace("return 2", "return 20")})
        self.run_session(self.repo, "now")
        self.records(("only-a", ["{path: mod.py, symbol: a}"]))
        ev = self.evidence()
        [hunk] = self.hunks(ev, "mod.py")  # git fused the two changes into one hunk
        self.assertEqual(hunk["state"], "unclaimed")
        self.assertEqual([b["state"] for b in hunk["blocks"]], ["covered", "unclaimed"])
        item = [i for i in ev["queue"] if i["kind"] == "unclaimed-hunk"][0]
        self.assertEqual(item["line"], 5)
        self.assertIn("explained by only-a", item["detail"])
        # Anchoring b as well explains the whole hunk.
        self.records(("only-a", ["{path: mod.py, symbol: a}"]), ("only-b", ["{path: mod.py, symbol: b}"]))
        [hunk] = self.hunks(self.evidence(), "mod.py")
        self.assertEqual(hunk["state"], "covered")
        self.assertEqual([b["by"] for b in hunk["blocks"]], [["only-a"], ["only-b"]])

    def test_pure_deletion_is_claimed_by_the_code_around_it(self):
        self.start({"mod.py": "def a():\n    x = 1\n    y = 2\n    return x\n\n\ndef b():\n    return 2\n"})
        self.commit(self.repo, "Drop y\n\nUnused.", {"mod.py": "def a():\n    x = 1\n    return x\n\n\ndef b():\n    return 2\n"})
        self.run_session(self.repo, "now")
        self.records(("a", ["{path: mod.py, symbol: a}"]))
        [hunk] = self.hunks(self.evidence(), "mod.py")
        self.assertEqual(hunk["state"], "covered")

    def test_binary_file_anchored_by_path_is_explained(self):
        self.start({"app.py": "x = 1\n"})
        (self.repo / "logo.bin").write_bytes(b"\x00\x01binary\x00" * 10)
        self.commit(self.repo, "Add logo\n\nA binary.")
        self.run_session(self.repo, "now")
        self.records(("assets", ["{path: logo.bin}"]))
        ev = self.evidence()
        anchor = [a for a in ev["anchors"] if a["path"] == "logo.bin"][0]
        self.assertEqual(anchor["status"], "path")
        self.assertEqual(self.hunks(ev, "logo.bin")[0]["state"], "covered")

    def test_pure_rename_and_mode_change_need_no_explanation(self):
        self.start({"old_name.py": "".join(f"line{i} = {i}\n" for i in range(20)), "tool.sh": "echo hi\n"})
        git(self.repo, "mv", "old_name.py", "new_name.py")
        (self.repo / "tool.sh").chmod(0o755)
        git(self.repo, "add", "tool.sh")
        git(self.repo, "commit", "-q", "-m", "Rename and chmod\n\nHousekeeping.")
        self.run_session(self.repo, "now")
        self.records()
        ev = self.evidence()
        files = {f["path"]: f for f in ev["files"]}
        self.assertEqual((files["new_name.py"]["noise"], files["tool.sh"]["noise"]), ("rename", "mode"))
        self.commit(self.repo, "Add a package marker\n\nEmpty.", {"pkg/__init__.py": ""})
        self.run_session(self.repo, "now")
        files = {f["path"]: f for f in self.evidence()["files"]}
        self.assertEqual(files["pkg/__init__.py"]["noise"], "empty")  # not a mode change
        self.assertEqual(ev["coverage"]["unclaimed"], 0)

    def test_ambiguous_symbol_is_reported(self):
        code = "class A:\n    def drain(self):\n        return 1\n\n\nclass B:\n    def drain(self):\n        return 2\n"
        self.start({"q.py": code})
        self.commit(self.repo, "Change B\n\nB.", {"q.py": code.replace("return 2", "return 3")})
        self.run_session(self.repo, "now")
        self.records(("queue", ["{path: q.py, symbol: drain}"]))
        ev = self.evidence()
        self.assertIn("ambiguous-anchor", {i["kind"] for i in ev["queue"]})
        self.assertEqual(self.hunks(ev, "q.py")[0]["state"], "unclaimed")  # A.drain was used, B changed

    def test_whitespace_only_changes_need_no_explanation(self):
        self.start({"mod.py": "def a():\n    return 1\n"})
        self.commit(self.repo, "Reindent\n\nTabs.", {"mod.py": "def a():\n        return 1\n"})
        self.run_session(self.repo, "now")
        self.records()
        self.assertEqual(self.evidence()["coverage"]["unclaimed"], 0)


class KeepEvidenceTests(EvidenceFixture):
    def publish(self):
        self.start({"app.py": "def main():\n    return 0\n"})
        self.commit(self.repo, "Return one\n\nOne.", {"app.py": "def main():\n    return 1\n"})
        self.run_session(self.repo, "now")
        self.records(("app", ["{path: app.py, symbol: main}"]))
        self.append_journal(self.fdir, "handoff", "Done.")
        self.run_session(self.repo, "publish")
        return json.loads((self.fdir / "evidence" / "evidence.json").read_text())

    def test_pruned_branch_keeps_its_evidence(self):
        before = self.publish()
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "merge", "-q", "--squash", "feat/fix")
        git(self.repo, "commit", "-q", "-m", "Return one (#1)")
        git(self.repo, "branch", "-q", "-D", "feat/fix")
        git(self.repo, "reflog", "expire", "--expire=now", "--all")
        git(self.repo, "gc", "-q", "--prune=now")
        ev = self.evidence()
        self.assertIn("aren't in this repository", ev["stale_note"])
        after = json.loads((self.fdir / "evidence" / "evidence.json").read_text())
        self.assertEqual(after, before)  # untouched on disk, so nothing bad syncs out
        self.assertEqual(len(after["commits"]), 1)
        index.rebuild()
        out = self.run_cli("show", before["commits"][0]["sha"][:9], cwd=self.repo)
        self.assertIn("feat--fix", out)

    def test_host_without_the_commits_keeps_another_hosts_evidence(self):
        before = self.publish()
        before["computed_on"] = "host-a"
        (self.fdir / "evidence" / "evidence.json").write_text(json.dumps(before))
        clone = self.tmp / "host-b-clone"
        # --no-local: copy only what main reaches, like a clone from a server the branch was never pushed to.
        git(self.tmp, "clone", "-q", "--no-local", "--single-branch", "-b", "main", str(self.repo), str(clone))
        ev = ingest.ingest_feature(self.pid, self.feature, repo=clone)
        self.assertFalse(ev["repo_available"])
        self.assertEqual(json.loads((self.fdir / "evidence" / "evidence.json").read_text())["computed_on"], "host-a")

    def test_reingest_is_a_no_op_on_disk(self):
        self.publish()
        path = self.fdir / "evidence" / "evidence.json"
        first = path.read_bytes()
        self.evidence()
        self.evidence()
        self.assertEqual(path.read_bytes(), first)

    def test_old_cumulative_patches_are_pruned(self):
        self.start({"app.py": "x = 0\n"})
        for i in range(1, 4):
            self.commit(self.repo, f"Step {i}\n\nS.", {"app.py": f"x = {i}\n"})
            self.run_session(self.repo, "now")
            self.records()
            ev = self.evidence()
        patches = sorted(p.name for p in (self.fdir / "evidence").glob("*.patch"))
        self.assertEqual(patches, sorted({ev["patch"]} | {leg["patch"] for leg in ev["legs"] if leg["patch"]}))

    def test_show_records_landings_only_on_the_default_branch(self):
        self.publish()
        feature_head = git(self.repo, "rev-parse", "feat/fix")
        git(self.repo, "checkout", "-q", "-b", "throwaway", "main")
        git(self.repo, "checkout", "-q", "feat/fix", "--", ".")
        git(self.repo, "commit", "-q", "-m", "Same tree, never merged")
        throwaway = git(self.repo, "rev-parse", "HEAD")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD^{tree}"), git(self.repo, "rev-parse", f"{feature_head}^{{tree}}"))
        self.assertEqual(squash.describe(throwaway, self.repo)["landed"], [])
        self.assertFalse((self.fdir / "evidence" / "landed.json").exists())
        out = self.run_cli("show", "deadbeef", cwd=self.repo)
        self.assertIn("not in any Debrief feature", out)

    def test_ingest_targets_are_checked(self):
        self.publish()
        self.run_cli("ingest", "--feature", "feat/fix")  # a branch name works as well as the feature id
        self.assertEqual(self.last_code, 0)
        self.run_cli("ingest", str(self.repo), "--feature", "nope")
        self.assertEqual(self.last_code, 1)
        self.run_cli("ingest", str(self.tmp / "missing"))
        self.assertEqual(self.last_code, 1)
        self.assertIn("No project has a feature nope", self.run_cli("ingest", "--feature", "nope"))
        self.assertEqual(self.last_code, 1)
        features = sorted(p.name for p in (self.archive / "projects" / self.pid / "features").iterdir())
        self.assertEqual(features, ["feat--fix"])


class AttributionTests(EvidenceFixture):
    def kinds(self, ev):
        return [(c["subject"], c["kind"]) for c in ev["commits"]]

    def logged(self):
        legs = sorted((self.fdir / "legs").glob("*.json"))
        return [c["subject"] for leg in legs for c in json.loads(leg.read_text())["commits"]]

    def test_amend_does_not_claim_a_developer_commit(self):
        self.start({"app.py": "x = 1\n"})
        self.run_session(self.repo, "close")
        self.commit(self.repo, "Developer README\n\nDocs.", {"README.md": "hi\n"})
        self.run_session(self.repo, "start")
        self.commit(self.repo, "Agent work\n\nFirst try.", {"app.py": "x = 2\n"})
        self.run_session(self.repo, "now")
        self.write(self.repo / "app.py", "x = 3\n")
        git(self.repo, "commit", "-q", "-a", "--amend", "-m", "Agent work\n\nAmended.")
        out = self.run_session(self.repo, "now")
        self.assertIn("Logged 1 commit", out)
        self.assertEqual(self.logged(), ["Agent work", "Agent work"])
        self.records()
        kinds = dict(self.kinds(self.evidence()))
        self.assertEqual(kinds["Developer README"], "developer")
        self.assertEqual(kinds["Agent work"], "agent")

    def test_merging_main_does_not_log_or_list_upstream_commits(self):
        self.start({"app.py": "x = 1\n"})
        self.commit(self.repo, "Agent one\n\nOne.", {"app.py": "x = 2\n"})
        self.run_session(self.repo, "now")
        git(self.repo, "checkout", "-q", "main")
        self.commit(self.repo, "Teammate one\n\nT1.", {"t1.txt": "1\n"})
        self.commit(self.repo, "Teammate two\n\nT2.", {"t2.txt": "2\n"})
        git(self.repo, "checkout", "-q", "feat/fix")
        git(self.repo, "merge", "-q", "--no-edit", "main")
        self.run_session(self.repo, "now")
        self.assertNotIn("Teammate one", self.logged())
        self.records()
        subjects = [s for s, _ in self.kinds(self.evidence())]
        self.assertNotIn("Teammate one", subjects)
        self.assertNotIn("Teammate two", subjects)

    def test_rebase_in_a_later_leg_counts_each_commit_once(self):
        self.start({"app.py": "x = 1\n"})
        self.commit(self.repo, "Leg one work\n\nL1.", {"f.py": "f = 1\n"})
        self.run_session(self.repo, "now")
        self.records()
        self.append_journal(self.fdir, "handoff", "Done.")
        self.run_session(self.repo, "publish")
        self.run_session(self.repo, "start")
        self.commit(self.repo, "Leg two work\n\nL2.", {"g.py": "g = 1\n"})
        self.run_session(self.repo, "now")
        git(self.repo, "checkout", "-q", "main")
        self.commit(self.repo, "Teammate change\n\nT.", {"t.txt": "t\n"})
        git(self.repo, "checkout", "-q", "feat/fix")
        git(self.repo, "rebase", "-q", "main")
        out = self.run_session(self.repo, "now")
        self.assertNotIn("Logged", out)  # rebased copies of logged work, and main's commit, aren't new
        ev = self.evidence()
        self.assertEqual(sorted(self.kinds(ev)), [("Leg one work", "agent"), ("Leg two work", "agent")])
        self.assertEqual(ev["stats"]["agent_commits"], 2)
        leg_one = [leg for leg in ev["legs"] if leg["leg_id"] == "leg-01"][0]
        head_shas = set(git(self.repo, "rev-list", "HEAD").split())
        self.assertTrue(set(leg_one["commits"]) <= head_shas, "leg one should list the rebased copy")

    def test_branch_with_no_commits_at_start(self):
        repo = self.tmp / "empty"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        self.repo = repo
        self.pid = self.init_project(repo)
        self.feature = "main"
        self.fdir = self.feature_dir(self.pid, "main")
        self.run_session(repo, "start")
        for i in range(3):
            self.commit(repo, f"Commit {i}\n\nBody.", {f"f{i}.py": f"v = {i}\n"})
        self.assertIn("Logged 3 commits", self.run_session(repo, "now"))
        self.records()
        ev = self.evidence()
        self.assertEqual(len(ev["commits"]), 3)
        self.assertEqual(len(ev["files"]), 3)

    def test_feature_closed_on_the_default_branch_ignores_later_commits(self):
        self.feature = "main"
        self.start({"app.py": "x = 1\n"}, branch="main-work")
        git(self.repo, "checkout", "-q", "main")
        self.fdir = self.feature_dir(self.pid, "main")
        self.run_session(self.repo, "start")
        self.commit(self.repo, "On main\n\nM.", {"m.py": "m = 1\n"})
        self.run_session(self.repo, "now")
        self.records()
        self.append_journal(self.fdir, "handoff", "Done.")
        self.run_session(self.repo, "publish")
        self.commit(self.repo, "Someone else later\n\nS.", {"s.py": "s = 1\n"})
        ev = self.evidence()
        self.assertEqual(ev["pending_commits"], [])
        self.assertEqual([c["subject"] for c in ev["commits"]], ["On main"])


class TestRunTests(EvidenceFixture):
    def run_with(self, claim, *argv):
        self.start({"app.py": "x = 1\n"})
        self.run_session(self.repo, "run", *argv)
        self.records()
        self.write(self.fdir / "tests.yaml", "tests:\n  - {id: t, validates: [x], claim: c, kind: unit, "
                                             f"command: {json.dumps(claim)}, claimed_result: pass}}\ngaps: []\n")
        return self.evidence()["tests"][0]

    def test_argv_runs_match_their_claim_text(self):
        test = self.run_with("python3 -c \"print('a b')\"", "python3", "-c", "print('a b')")
        self.assertEqual(test["status"], "verified_pass")

    def test_a_longer_command_does_not_verify_a_shorter_claim(self):
        # The old matcher took "echo test-lint" (or any run starting "echo test ") as a run of "echo test".
        test = self.run_with("echo test", "echo test-lint")
        self.assertEqual(test["status"], "claimed_only")

    def test_extra_arguments_do_not_verify_a_claim(self):
        test = self.run_with("true", "true --version")
        self.assertEqual(test["status"], "claimed_only")

    def test_quiet_flags_do_not_matter(self):
        test = self.run_with("true", "true -q")
        self.assertEqual(test["status"], "verified_pass")

    def test_a_run_before_committing_is_current(self):
        self.start({"app.py": "x = 1\n"})
        self.write(self.repo / "app.py", "x = 2\n")
        self.write(self.repo / "new.py", "n = 1\n")
        self.run_session(self.repo, "run", "true")
        self.commit(self.repo, "Commit what was tested\n\nT.")
        self.run_session(self.repo, "now")
        self.records()
        self.write(self.fdir / "tests.yaml", "tests:\n  - {id: t, validates: [x], claim: c, kind: unit, command: 'true', "
                                             "claimed_result: pass}\ngaps: []\n")
        self.assertTrue(self.evidence()["tests"][0]["current"])
        self.commit(self.repo, "Change after the run\n\nU.", {"app.py": "x = 3\n"})
        self.run_session(self.repo, "now")
        self.assertFalse(self.evidence()["tests"][0]["current"])


class RuleTests(unittest.TestCase):
    def fired(self, line, path="svc/client.py"):
        from debrief import diffparse

        patch = f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1,0 +1,1 @@\n+{line}\n"
        files = diffparse.parse(patch)
        return {f["rule"] for f in rules.scan(rules.load(None), path, files[0].hunks)}

    def test_retry_rule_wants_retry_logic_not_names(self):
        for line in ("self.max_attempts = max_attempts", "self.backoff = backoff", "def __init__(self, retries=3):"):
            self.assertNotIn("retry", self.fired(line), line)
        for line in ("for attempt in range(self.max_attempts):", "@retry(stop=stop_after_attempt(3))",
                     "while attempts < max_attempts:", "delay = base * 2 ** attempt", "import tenacity",
                     "session.mount('https://', HTTPAdapter(max_retries=Retry(total=3)))"):
            self.assertIn("retry", self.fired(line), line)

    def test_narrow_excepts_are_not_swallowed_errors(self):
        from debrief import diffparse

        path = "svc/x.py"
        patch = (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1,0 +1,4 @@\n"
                 "+try:\n+    os.chmod(p, 0o600)\n+except OSError:\n+    pass\n")
        files = diffparse.parse(patch)
        self.assertNotIn("swallowed-error", {f["rule"] for f in rules.scan(rules.load(None), path, files[0].hunks)})

    def test_two_line_swallowed_error(self):
        from debrief import diffparse

        path = "svc/x.py"
        patch = (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1,0 +1,4 @@\n"
                 "+try:\n+    go()\n+except Exception:\n+    pass\n")
        files = diffparse.parse(patch)
        self.assertIn("swallowed-error", {f["rule"] for f in rules.scan(rules.load(None), path, files[0].hunks)})


if __name__ == "__main__":
    unittest.main()

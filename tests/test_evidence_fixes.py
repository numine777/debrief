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

class AttributionTests(EvidenceFixture):
    def kinds(self, ev):
        return [(c["subject"], c["kind"]) for c in ev["commits"]]

    def logged(self):
        legs = sorted((self.fdir / "legs").glob("*.json"))
        return [c["subject"] for leg in legs for c in json.loads(leg.read_text())["commits"]]

class TestRunTests(EvidenceFixture):
    def run_with(self, claim, *argv):
        self.start({"app.py": "x = 1\n"})
        self.run_session(self.repo, "run", *argv)
        self.records()
        self.write(self.fdir / "tests.yaml", "tests:\n  - {id: t, validates: [x], claim: c, kind: unit, "
                                             f"command: {json.dumps(claim)}, claimed_result: pass}}\ngaps: []\n")
        return self.evidence()["tests"][0]

class RuleTests(unittest.TestCase):
    def fired(self, line, path="svc/client.py"):
        from debrief import diffparse

        patch = f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1,0 +1,1 @@\n+{line}\n"
        files = diffparse.parse(patch)
        return {f["rule"] for f in rules.scan(rules.load(None), path, files[0].hunks)}

if __name__ == "__main__":
    unittest.main()

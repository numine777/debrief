"""End-to-end ingest: coverage, commit kinds, flags, test evidence, squash mapping."""

import json
import unittest

from tests.helpers import IsolatedTestCase, git

from debrief import index, ingest, records, squash

QUEUE_V1 = '''import time


class RetryQueue:
    def __init__(self):
        self.items = []

    def drain(self, deadline):
        while self.items:
            if time.time() > deadline:
                break
            self.items.pop()
            time.sleep(0.01)
'''

QUEUE_V2 = QUEUE_V1 + '''
    def add(self, item):
        self.items.append(item)
'''

OTHER_V1 = '''def run():
    return "ok"
'''

OTHER_V2 = '''def run():
    return "ok!"
'''

OTHER_V3 = OTHER_V2 + '''

def poll(check):
    while True:
        if check():
            return
'''

SYSTEMS = {
    "retry-queue": """---
id: retry-queue
title: Retry queue
change: new
depends_on: []
anchors:
  - {path: queue.py, symbol: RetryQueue, role: core}
critical_paths:
  - id: drain-loop
    kind: loop
    anchor: {path: queue.py, symbol: RetryQueue.drain}
    invariant: "Stops when empty or past the deadline."
decisions: []
---

## Purpose
P.

## Change
New.

## How it works
H.

## Limitations
L.
""",
    "other-system": """---
id: other-system
title: Other
change: modified
depends_on: []
anchors:
  - {path: other.py, symbol: run}
  - {path: other.py, symbol: poll}
  - {path: notes.md}
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
""",
    "ghost": """---
id: ghost
title: Ghost
change: touched
depends_on: []
anchors:
  - {path: queue.py, symbol: NoSuchThing}
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
""",
}

TESTS_YAML = """tests:
  - id: retry-tests
    validates: [retry-queue/drain-loop]
    claim: "Drain stops at the deadline."
    kind: unit
    anchors: []
    command: "python3 -c 'print(42)'"
    claimed_result: pass
  - id: other-tests
    validates: [other-system]
    claim: "Run returns ok."
    kind: unit
    anchors: []
    command: "pytest tests/test_other.py"
    claimed_result: pass
gaps: []
"""


class IngestScenario(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.repo = self.make_repo(files={"queue.py": "# queue\n", "util.py": "x = 1\n", "other.py": OTHER_V1,
                                          "notes.md": "# Notes\n"})
        git(self.repo, "checkout", "-q", "-b", "feat/retry")
        self.pid = self.init_project(self.repo)
        self.fdir = self.feature_dir(self.pid, "feat--retry")
        self.run_session(self.repo, "start")
        self.c1 = self.commit(self.repo, "Add a deadline-aware retry queue\n\nDrain stops at the deadline.",
                              {"queue.py": QUEUE_V1})
        self.c2 = self.commit(self.repo, "Tweak util", {"util.py": "x = 2\n"})
        self.c3 = self.commit(self.repo, "Add queue items and change run\n\nTwo unrelated changes.",
                              {"queue.py": QUEUE_V2, "other.py": OTHER_V2, "notes.md": "# Notes\n\nMore.\n"})
        self.run_session(self.repo, "run", "python3 -c 'print(42)'")
        self.append_journal(self.fdir, "handoff", "Done.")
        self.run_session(self.repo, "close")
        self.c4 = self.commit(self.repo, "Developer adds dev.py\n\nBy hand.", {"dev.py": "def dev():\n    pass\n"})
        self.run_session(self.repo, "start")
        self.c5 = self.commit(self.repo, "Add polling helper\n\nPolls until the check passes.", {"other.py": OTHER_V3})
        self.run_session(self.repo, "now")
        for name, text in SYSTEMS.items():
            self.write(self.fdir / "systems" / f"{name}.md", text)
        self.write(self.fdir / "tests.yaml", TESTS_YAML)
        self.write(self.fdir / "brief.md", "---\nfeature_id: feat--retry\ntitle: Retry\nstatus: in_progress\n"
                                           "incidental: [util.py]\n---\n\n## Intent\nI\n")

    def evidence(self):
        return ingest.ingest_feature(self.pid, "feat--retry")

    def test_full_evidence(self):
        ev = self.evidence()
        self.assertTrue(ev["repo_available"])
        self.assertEqual(ev["head"], self.c5)
        files = {f["path"]: f for f in ev["files"]}
        self.assertEqual(set(files), {"queue.py", "util.py", "other.py", "notes.md", "dev.py"})
        self.assertEqual({h["state"] for h in files["queue.py"]["hunks"]}, {"covered"})
        self.assertEqual({h["state"] for h in files["util.py"]["hunks"]}, {"incidental"})
        self.assertEqual({h["state"] for h in files["dev.py"]["hunks"]}, {"unclaimed"})
        self.assertEqual({h["state"] for h in files["notes.md"]["hunks"]}, {"weak"})
        cov = ev["coverage"]
        self.assertEqual((cov["unclaimed"], cov["weak"]), (1, 1))
        # Commit kinds come from bin/session's log, never from the commits.
        kinds = {c["sha"]: c["kind"] for c in ev["commits"]}
        self.assertEqual(kinds, {self.c1: "agent", self.c2: "agent", self.c3: "agent", self.c4: "developer",
                                 self.c5: "agent"})
        by_sha = {c["sha"]: c for c in ev["commits"]}
        self.assertTrue(by_sha[self.c2]["thin_message"])
        self.assertFalse(by_sha[self.c1]["thin_message"])
        self.assertTrue(by_sha[self.c3]["non_atomic"])
        self.assertEqual(set(by_sha[self.c3]["systems"]), {"retry-queue", "other-system"})
        # Flags: the declared drain loop is quiet, the new poll loop is not.
        undeclared = [i for i in ev["queue"] if i["kind"] == "undeclared-critical"]
        self.assertTrue(any(i["path"] == "other.py" and i["rule"] == "loop" for i in undeclared), undeclared)
        self.assertFalse(any(i["path"] == "queue.py" and i["rule"] in ("loop", "sleep") for i in undeclared))
        tests = {t["id"]: t for t in ev["tests"]}
        self.assertEqual(tests["retry-tests"]["status"], "verified_pass")
        self.assertEqual(tests["other-tests"]["status"], "claimed_only")
        kinds_in_queue = {i["kind"] for i in ev["queue"]}
        self.assertTrue({"unclaimed-hunk", "weak-claim", "stale-anchor", "claim-only-test", "non-atomic-commit",
                         "thin-commit-message", "undeclared-critical"} <= kinds_in_queue, kinds_in_queue)
        order = [i["severity"] for i in ev["queue"]]
        self.assertEqual(order, sorted(order, key=lambda s: {"high": 0, "medium": 1, "low": 2}[s]))
        stale = [a for a in ev["anchors"] if a["status"] == "stale"]
        self.assertEqual([a["owner"] for a in stale], ["ghost"])
        cps = {c["ref"]: c for c in ev["critical_paths"]}
        self.assertEqual(cps["retry-queue/drain-loop"]["status"], "tested")
        self.assertTrue(cps["retry-queue/drain-loop"]["verified"])
        # Snapshots survive the branch: patches, commits and blobs live in the archive.
        evdir = self.fdir / "evidence"
        self.assertTrue((evdir / ev["patch"]).exists())
        commit = json.loads((evdir / "commits" / f"{self.c3}.json").read_text())
        self.assertIn("+    def add(self, item):", commit["patch"])
        self.assertEqual(commit["leg_id"], "leg-01")
        head_blob = files["queue.py"]["new_blob"]
        self.assertEqual((evdir / "blobs" / head_blob).read_text(), QUEUE_V2)
        self.assertEqual(ev["stats"]["developer_commits"], 1)

    def test_index_lookup_and_search(self):
        self.evidence()
        rows = index.lookup_commit(self.c1[:10])
        self.assertEqual([(r["feature_id"], r["kind"]) for r in rows], [("feat--retry", "agent")])
        hits = index.search("RetryQueue")
        self.assertTrue(any(h["kind"] == "system" and h["ref"] == "retry-queue" for h in hits), hits)
        features = index.list_features()
        self.assertEqual(features[0]["title"], "Retry")
        self.assertEqual(features[0]["open_legs"], 1)
        out = self.run_cli("show", self.c3[:12], cwd=self.repo)
        self.assertIn("feat--retry", out)
        self.assertIn("leg-01", out)
        self.assertIn("other-system", out)

    def test_rebased_commits_still_count_as_agent_commits(self):
        git(self.repo, "checkout", "-q", "main")
        self.commit(self.repo, "Upstream change", {"upstream.txt": "u\n"})
        git(self.repo, "checkout", "-q", "feat/retry")
        git(self.repo, "rebase", "-q", "main")
        ev = self.evidence()
        kinds = {c["subject"]: c["kind"] for c in ev["commits"]}
        self.assertEqual(kinds["Add a deadline-aware retry queue"], "agent")
        self.assertEqual(kinds["Developer adds dev.py"], "developer")
        rebased = [c for c in ev["commits"] if c["subject"] == "Add polling helper"][0]
        self.assertEqual(rebased["rebased_from"], self.c5)
        self.assertNotIn("upstream.txt", {f["path"] for f in ev["files"]})

    def test_merged_upstream_changes_drop_out(self):
        git(self.repo, "checkout", "-q", "main")
        self.commit(self.repo, "Upstream change", {"upstream.txt": "u\n"})
        git(self.repo, "checkout", "-q", "feat/retry")
        git(self.repo, "merge", "-q", "--no-edit", "main")
        ev = self.evidence()
        self.assertNotIn("upstream.txt", {f["path"] for f in ev["files"]})
        self.assertIn("merge", {c["kind"] for c in ev["commits"]})

    def test_uncommitted_work_is_shown_as_worktree(self):
        self.write(self.repo / "queue.py", QUEUE_V2 + "\n# wip\n")
        self.write(self.repo / "fresh.py", "def fresh():\n    return 1\n")
        ev = self.evidence()
        self.assertEqual(ev["head"], "WORKTREE")
        files = {f["path"]: f for f in ev["files"]}
        self.assertEqual(files["fresh.py"]["status"], "A")
        blob = files["fresh.py"]["new_blob"]
        self.assertEqual((self.fdir / "evidence" / "blobs" / blob).read_text(), "def fresh():\n    return 1\n")
        self.assertEqual(ev["legs"][-1]["head"], "WORKTREE")

    def test_squash_merge_maps_back_by_tree(self):
        self.write_records(self.fdir, systems=[])
        for name, text in SYSTEMS.items():
            self.write(self.fdir / "systems" / f"{name}.md", text)
        self.run_session(self.repo, "publish", "--force")
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "merge", "-q", "--squash", "feat/retry")
        git(self.repo, "commit", "-q", "-m", "Retry queue (#12)")
        squash_sha = git(self.repo, "rev-parse", "HEAD")
        matches = squash.match_commit(self.repo, squash_sha, self.pid)
        self.assertEqual(matches[0]["feature_id"], "feat--retry")
        self.assertEqual(matches[0]["method"], "tree")
        info = squash.describe(squash_sha, self.repo)
        self.assertEqual(info["landed"][0]["feature_id"], "feat--retry")
        landed = json.loads((self.fdir / "evidence" / "landed.json").read_text())
        self.assertEqual(landed["commits"][0]["sha"], squash_sha)
        self.assertEqual(index.lookup_commit(squash_sha)[0]["match"], "landed")

    def test_partial_match_by_files(self):
        self.evidence()
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "checkout", "-q", "feat/retry", "--", "queue.py")
        git(self.repo, "commit", "-q", "-m", "Cherry-pick only the queue")
        sha = git(self.repo, "rev-parse", "HEAD")
        matches = squash.match_commit(self.repo, sha, self.pid)
        self.assertEqual(matches[0]["method"], "files")
        self.assertLess(matches[0]["confidence"], 1.0)

    def test_records_without_repo_keep_previous_evidence(self):
        first = self.evidence()
        registry = self.archive / ".registry.json"
        registry.write_text(json.dumps({"projects": {}}))
        import shutil

        shutil.move(str(self.repo), str(self.tmp / "moved"))
        again = ingest.ingest_feature(self.pid, "feat--retry")
        self.assertFalse(again["repo_available"])
        self.assertEqual(again["coverage"], first["coverage"])

    def test_squash_record(self):
        self.evidence()
        out = squash.squash_feature(self.pid, "feat--retry")
        text = (out / "record.md").read_text()
        self.assertIn("## Legs", text)
        self.assertIn("Handoff", text)
        data = json.loads((out / "index.json").read_text())
        self.assertEqual(data["legs"][0]["leg_id"], "leg-01")
        self.assertIn(self.c1, data["legs"][0]["commits"])
        self.assertTrue(records.load_feature(self.fdir)["legs"])


if __name__ == "__main__":
    unittest.main()

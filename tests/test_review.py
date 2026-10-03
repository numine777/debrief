"""The review loop: comments, prompts, feedback, marks, Close leg, export, sync, compaction, watcher."""

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from tests.helpers import IsolatedTestCase, git

from debrief import comments, compaction, config, export, ingest, records, service, watch
from debrief.api import Api
from debrief.review import ReviewApp, request_close
from debrief.server import Request

BASE_TEXT = "".join(f"line {i}\n" for i in range(1, 21))


def req(app, method, path, body=None):
    headers = {"Host": "127.0.0.1:7319"}
    data = b""
    if method != "GET":
        headers.update({"X-Debrief": "1", "Content-Type": "application/json"})
        data = json.dumps(body or {}).encode()
    resp = app.handle(Request(method, path, headers, data))
    try:
        return resp.status, json.loads(resp.body.decode())
    except ValueError:
        return resp.status, resp.body


class ReviewLoopTests(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.repo = self.make_repo(files={"app.py": BASE_TEXT})
        git(self.repo, "checkout", "-q", "-b", "feat/review")
        self.pid = self.init_project(self.repo)
        self.fdir = self.feature_dir(self.pid, "feat--review")
        self.run_session(self.repo, "start")
        self.commit(self.repo, "Change line five\n\nBody.", {"app.py": BASE_TEXT.replace("line 5\n", "line five\n")})
        self.run_session(self.repo, "now")
        self.write_records(self.fdir)
        self.ev = ingest.ingest_feature(self.pid, "feat--review")
        self.app = ReviewApp(Api(self.archive), 7319)
        self.base = f"/api/v1/projects/{self.pid}/features/feat--review"
        self.file = self.ev["files"][0]

    def anchor(self, line, text, side="new"):
        return {"scope": "feature", "path": "app.py", "side": side, "line": line, "text": text,
                "commit": self.ev["head_commit"], "blob": self.file["new_blob"] if side == "new" else self.file["old_blob"]}

    def test_comment_lifecycle_and_prompt(self):
        status, data = req(self.app, "POST", f"{self.base}/comments",
                           {"anchor": self.anchor(5, "line five"), "body": "Explain why five is spelled out.", "visibility": "private"})
        self.assertEqual(status, 201, data)
        cid = data["comment"]["id"]
        self.assertTrue((self.archive / ".viewer" / self.pid / "feat--review" / "comments.json").exists())
        self.assertFalse((self.fdir / "comments.json").exists())
        status, data = req(self.app, "POST", f"{self.base}/comments", {"anchor": self.anchor(5, "x"), "body": "  "})
        self.assertEqual(status, 400)
        feature = req(self.app, "GET", self.base)[1]
        self.assertEqual(feature["comments"][0]["current"], {"line": 5, "outdated": False, "moved": False,
                                                             "head": self.ev["head"]})
        status, data = req(self.app, "POST", f"{self.base}/prompt", {})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["prompt"], (
            f"Review comments on feat/review (head {self.ev['head_commit'][:7]}). Address each one, log what\n"
            "you change in your journal, and commit each fix atomically.\n\n"
            "1. app.py:5\n   > line five\n   Explain why five is spelled out.\n"))
        self.assertEqual(req(self.app, "GET", f"{self.base}/comments")[1]["comments"][0]["state"], "sent")
        status, _ = req(self.app, "POST", f"{self.base}/prompt", {})
        self.assertEqual(status, 400)  # nothing open any more
        status, data = req(self.app, "PATCH", f"{self.base}/comments/{cid}", {"state": "resolved"})
        self.assertEqual(data["comment"]["state"], "resolved")
        status, data = req(self.app, "DELETE", f"{self.base}/comments/{cid}")
        self.assertEqual(status, 200)
        self.assertEqual(req(self.app, "GET", f"{self.base}/comments")[1]["comments"], [])

    def test_shared_comments_commit_to_the_archive(self):
        status, data = req(self.app, "POST", f"{self.base}/comments",
                           {"anchor": self.anchor(3, "line 3"), "body": "Shared note", "visibility": "shared"})
        self.assertEqual(status, 201)
        stored = json.loads((self.fdir / "comments.json").read_text())
        self.assertEqual(stored["comments"][0]["body"], "Shared note")
        log = git(self.archive / "projects" / self.pid, "log", "--format=%s")
        self.assertIn("Add a review comment on feat--review", log)
        cid = data["comment"]["id"]
        status, data = req(self.app, "PATCH", f"{self.base}/comments/{cid}", {"visibility": "private"})
        self.assertEqual(data["comment"]["visibility"], "private")
        self.assertEqual(json.loads((self.fdir / "comments.json").read_text())["comments"], [])

    def test_comments_follow_moved_lines_and_go_outdated(self):
        c_move = comments.create(self.pid, "feat--review", {"anchor": self.anchor(10, "line 10"), "body": "a"}, "dev", self.archive)
        c_gone = comments.create(self.pid, "feat--review", {"anchor": self.anchor(5, "line five"), "body": "b"}, "dev", self.archive)
        text = "header\nheader 2\n" + BASE_TEXT.replace("line 5\n", "line FIVE!\n")
        self.commit(self.repo, "Shift and rewrite", {"app.py": text})
        self.run_session(self.repo, "now")
        ev = ingest.ingest_feature(self.pid, "feat--review")
        found = {c["id"]: c for c in comments.locate(comments.load_all(self.pid, "feat--review", self.archive), self.fdir, ev)}
        self.assertEqual(found[c_move["id"]]["current"]["line"], 12)
        self.assertTrue(found[c_move["id"]]["current"]["moved"])
        self.assertTrue(found[c_gone["id"]]["current"]["outdated"])
        prompt = comments.build_prompt(list(found.values()), {"branch": "feat/review"}, ev["head_commit"])
        self.assertIn("app.py:12 (was line 10 when reviewed at", prompt)
        self.assertIn("app.py (was line 5 when reviewed at", prompt)
        self.assertIn("that line has since changed", prompt)

    def test_queued_feedback_reaches_the_agent(self):
        req(self.app, "POST", f"{self.base}/comments", {"anchor": self.anchor(5, "line five"), "body": "Fix it."})
        status, data = req(self.app, "POST", f"{self.base}/prompt", {"queue": True})
        self.assertTrue(data["queued"])
        feedback = (self.fdir / "feedback.md").read_text()
        self.assertIn("1. app.py:5", feedback)
        out = self.run_session(self.repo, "now")
        self.assertIn("FEEDBACK: 1 new review item", out)
        self.assertIn("feedback.md", out)

    def test_close_request_from_the_viewer_reaches_the_agent(self):
        status, data = req(self.app, "POST", f"{self.base}/close-request", {"note": "PR is approved"})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["leg_id"], "leg-01")
        self.assertIn("ai-session-closeout/SKILL.md", data["prompt"])
        out = self.run_session(self.repo, "now")
        self.assertIn("close out leg-01 now", out)
        self.assertIn("PR is approved", out)
        self.assertIn("Request close of leg-01", git(self.archive / "projects" / self.pid, "log", "--format=%s"))

    def test_close_request_needs_an_open_leg(self):
        self.append_journal(self.fdir, "handoff", "Done.")
        self.run_session(self.repo, "publish")
        status, data = req(self.app, "POST", f"{self.base}/close-request", {})
        self.assertEqual(status, 400)

    def test_cli_request_close(self):
        out = self.run_cli("request-close", cwd=self.repo)
        self.assertIn("Requested close of leg-01", out)
        self.assertTrue(records.load_legs(self.fdir)[0]["close_requested_at"])

    def test_marks_clear_when_the_hunk_changes(self):
        hunk = self.file["hunks"][0]["id"]
        status, data = req(self.app, "POST", f"{self.base}/marks", {"hunk_id": hunk, "reviewed": True})
        self.assertEqual(data["marks"], [hunk])
        feature = req(self.app, "GET", self.base)[1]
        self.assertEqual(feature["marks"], [hunk])
        self.assertEqual(feature["review"]["reviewed"], 1)
        self.commit(self.repo, "Change it again", {"app.py": BASE_TEXT.replace("line 5\n", "line 5!\n")})
        self.run_session(self.repo, "now")
        ingest.ingest_feature(self.pid, "feat--review")
        feature = req(self.app, "GET", self.base)[1]
        self.assertEqual(feature["marks"], [])
        self.assertEqual(feature["review"]["reviewed"], 0)

    def test_export_is_self_contained_and_hash_locked(self):
        comments.create(self.pid, "feat--review", {"anchor": self.anchor(5, "line five"), "body": "private!"}, "dev", self.archive)
        comments.create(self.pid, "feat--review", {"anchor": self.anchor(5, "line five"), "body": "shared note", "visibility": "shared"},
                        "dev", self.archive)
        html = export.render(Api(self.archive), self.pid, "feat--review")
        csp = re.search(r'http-equiv="Content-Security-Policy" content="([^"]+)"', html).group(1)
        self.assertIn("default-src 'none'", csp)
        self.assertNotIn("unsafe-inline", csp)
        scripts = re.findall(r"<script>(.*?)</script>", html, flags=re.S)
        self.assertEqual(len(scripts), len(export.SCRIPTS) + 1)
        for script in scripts:
            digest = "'sha256-" + base64.b64encode(hashlib.sha256(script.encode()).digest()).decode() + "'"
            self.assertIn(digest, csp)
            self.assertNotRegex(script.lower(), r"<(!--|/?script)")
        style = re.search(r"<style>(.*?)</style>", html, flags=re.S).group(1)
        self.assertIn("data:font/woff2;base64,", style)
        self.assertNotIn("private!", html)
        self.assertIn("shared note", html)
        self.assertNotRegex(html, r"(src|href)=\"https?:")
        node = shutil.which("node")
        if node:
            for script in scripts:
                proc = subprocess.run([node, "--check", "-"], input=script, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                self.assertEqual(proc.returncode, 0, proc.stderr[:400])
        status, body = req(self.app, "GET", f"{self.base}/export")
        self.assertEqual(status, 200)
        out = self.run_cli("export", "feat--review", "--project", self.pid, "-o", str(self.tmp / "out.html"))
        self.assertIn("Wrote", out)
        self.assertTrue((self.tmp / "out.html").exists())

    def test_watcher_ingests_changed_records_and_refs(self):
        app = ReviewApp(Api(self.archive), 7319)
        w = watch.Watcher(app, None, self.archive)
        w.tick()  # baseline
        before = app.generation
        self.assertEqual(w.tick(), 0)
        self.commit(self.repo, "Another change", {"app.py": BASE_TEXT.replace("line 7\n", "line seven\n")})
        self.assertGreaterEqual(w.tick(), 1)
        self.assertGreater(app.generation, before)
        self.append_journal(self.fdir, "finding", "New entry.")
        self.assertEqual(w.tick(), 1)


class SyncAndServiceTests(IsolatedTestCase):
    def test_sync_cli_sets_remote_and_pushes(self):
        repo = self.make_repo()
        pid = self.init_project(repo)
        remote = self.tmp / "archive-remote.git"
        git(self.tmp, "init", "-q", "--bare", "-b", "main", str(remote))
        out = self.run_cli("sync", pid, "--remote", str(remote))
        self.assertIn("pushed", out)
        self.assertIn("Register", git(remote, "log", "--format=%s", "main"))
        out = self.run_cli("sync", pid, "--remote", "git@github.com:me/archive.git")
        self.assertIn("not on the residency allowlist", out)

    def test_service_files(self):
        path, text, start, stop = service.target()
        self.assertIn("serve --watch", text)
        out = self.run_cli("service", "print")
        self.assertIn("serve", out)


class _FakeModel(BaseHTTPRequestHandler):
    seen = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _FakeModel.seen.append((self.path, {k.lower(): v for k, v in self.headers.items()}, body))
        if self.path.endswith("/chat/completions"):
            reply = {"choices": [{"message": {"content": "- summarized (openai)"}}]}
        else:
            reply = {"content": [{"type": "text", "text": "- summarized (anthropic)"}]}
        data = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class CompactionTests(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.server = HTTPServer(("127.0.0.1", 0), _FakeModel)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        _FakeModel.seen = []
        repo = self.make_repo()
        git(repo, "checkout", "-q", "-b", "feat/c")
        self.pid = self.init_project(repo)
        self.fdir = self.feature_dir(self.pid, "feat--c")
        self.run_session(repo, "start")
        self.append_journal(self.fdir, "plan", "Do the thing.")
        self.append_journal(self.fdir, "handoff", "Did the thing.")
        self.write_records(self.fdir)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def configure(self, api, auth=""):
        self.write(self.tmp / "config" / "config", (
            "[experimental]\ncompaction = true\n[compaction]\nprovider = local\n"
            f"[provider.local]\napi = {api}\nbase_url = http://127.0.0.1:{self.port}/v1\nmodel = small\n"
            f"api_key_env = TEST_MODEL_KEY\n{auth}"))
        os.environ["TEST_MODEL_KEY"] = "secret"
        self.addCleanup(os.environ.pop, "TEST_MODEL_KEY", None)

    def test_off_by_default(self):
        with self.assertRaises(compaction.CompactionError) as ctx:
            compaction.compact_feature(self.pid, "feat--c", cfg=config.load())
        self.assertIn("experimental and off", str(ctx.exception))

    def test_openai_adapter(self):
        self.configure("openai", "auth_header = api-key\n")
        out = compaction.compact_feature(self.pid, "feat--c")
        text = out.read_text()
        self.assertIn("model_written: true", text)
        self.assertIn("summarized (openai)", text)
        path, headers, body = _FakeModel.seen[0]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(headers.get("api-key"), "secret")
        self.assertEqual(body["model"], "small")
        self.assertIn("Do the thing.", body["messages"][1]["content"])

    def test_anthropic_adapter(self):
        self.configure("anthropic")
        out = compaction.compact_feature(self.pid, "feat--c")
        self.assertIn("summarized (anthropic)", out.read_text())
        path, headers, body = _FakeModel.seen[0]
        self.assertEqual(path, "/v1/messages")
        self.assertEqual(headers.get("x-api-key"), "secret")
        self.assertEqual(headers.get("anthropic-version"), "2023-06-01")

    def test_endpoint_must_be_allowlisted(self):
        self.configure("openai")
        cfg_path = self.tmp / "config" / "config"
        cfg_path.write_text(cfg_path.read_text().replace(f"http://127.0.0.1:{self.port}/v1", "https://api.example.com/v1"))
        with self.assertRaises(Exception) as ctx:
            compaction.compact_feature(self.pid, "feat--c")
        self.assertIn("allowlist", str(ctx.exception))
        self.assertEqual(_FakeModel.seen, [])

    def test_missing_key_is_reported(self):
        self.configure("openai")
        os.environ.pop("TEST_MODEL_KEY")
        with self.assertRaises(compaction.CompactionError) as ctx:
            compaction.compact_feature(self.pid, "feat--c")
        self.assertIn("TEST_MODEL_KEY", str(ctx.exception))

    def test_squash_cli_with_model(self):
        self.configure("openai")
        out = self.run_cli("squash", "feat--c", "--project", self.pid, "--model")
        self.assertIn("record.md", out)
        self.assertIn("model-written", out)
        self.assertTrue((self.fdir / "squash" / "compaction.md").exists())


if __name__ == "__main__":
    unittest.main()

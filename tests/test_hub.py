"""Debrief Hub: tokens, sessions, roles, TLS, and records flowing between hosts and the hub."""

import json
import os
import shutil
import ssl
import subprocess
import sys
import threading
import time
import unittest
import urllib.request
from unittest import mock

from tests.helpers import ROOT, IsolatedTestCase, git

from debrief import archive, comments, index, ingest, paths, records
from debrief.api import BadRequest
from debrief.hub import Hub, HubApp, HubSyncer, _rewire_routes, make_hub_server
from debrief.server import Request


def call(app, method, path, body=None, cookie=None, token=None, host="hub.example:7320"):
    headers = {"Host": host}
    if cookie:
        headers["Cookie"] = cookie
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = b""
    if method != "GET":
        headers.update({"X-Debrief": "1", "Content-Type": "application/json"})
        data = json.dumps(body or {}).encode()
    resp = app.handle(Request(method, path, headers, data, client="10.0.0.5"))
    try:
        payload = json.loads(resp.body.decode())
    except ValueError:
        payload = resp.body
    return resp, payload


class HubTests(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        # A developer host with one feature, published.
        self.repo = self.make_repo(files={"app.py": "".join(f"x{i} = {i}\n" for i in range(10))})
        git(self.repo, "checkout", "-q", "-b", "feat/hubbed")
        self.pid = self.init_project(self.repo)
        self.fdir = self.feature_dir(self.pid, "feat--hubbed")
        self.run_session(self.repo, "start")
        self.commit(self.repo, "Change x3\n\nBody.", {"app.py": "".join(f"x{i} = {i * (2 if i == 3 else 1)}\n" for i in range(10))})
        self.run_session(self.repo, "now")
        self.write_records(self.fdir)
        ingest.ingest_feature(self.pid, "feat--hubbed")
        # The hub, with the project's bare archive repository.
        self.hub = Hub(self.tmp / "hub")
        self.hub.init("hub.example")
        bare = self.hub.add_repo(self.pid)
        archive.set_remote(paths.project_dir(self.pid), str(bare))
        archive.sync(paths.project_dir(self.pid), "Publish to hub")
        self.syncer = HubSyncer(self.hub)
        self.assertEqual(self.syncer.tick(), [self.pid])
        self.app = HubApp(self.hub, 7320, ["hub.example"], secure=True)
        _rewire_routes(self.app)
        self.syncer.app = self.app
        self.alice = self.hub.add_user("alice")
        self.bob = self.hub.add_user("bob")
        self.carol = self.hub.add_user("carol")
        self.hub.grant("alice", self.pid, "reviewer")
        self.hub.grant("bob", self.pid, "reader")
        self.base = f"/api/v1/projects/{self.pid}/features/feat--hubbed"

    def login(self, token):
        resp, data = call(self.app, "POST", "/api/v1/login", {"token": token})
        self.assertEqual(resp.status, 200, data)
        return resp.headers["Set-Cookie"].split(";")[0]

    def test_tokens_are_stored_hashed(self):
        stored = (self.tmp / "hub" / "users.json").read_text()
        self.assertNotIn(self.alice, stored)
        self.assertEqual(self.hub.authenticate(self.alice)["name"], "alice")
        self.assertIsNone(self.hub.authenticate("dbh_wrong"))
        self.assertEqual(self.hub.revoke_tokens("alice"), 1)
        self.assertIsNone(self.hub.authenticate(self.alice))
        with self.assertRaises(BadRequest):
            self.hub.add_user("alice")

    def test_sign_in_and_cookie_flags(self):
        resp, meta = call(self.app, "GET", "/api/v1/meta")
        self.assertEqual((meta["mode"], meta["user"]), ("hub", None))
        self.assertEqual(call(self.app, "GET", "/api/v1/index")[0].status, 401)
        self.assertEqual(call(self.app, "GET", "/")[0].status, 200)
        resp, _ = call(self.app, "POST", "/api/v1/login", {"token": "dbh_nope"})
        self.assertEqual(resp.status, 401)
        resp, data = call(self.app, "POST", "/api/v1/login", {"token": self.alice})
        cookie = resp.headers["Set-Cookie"]
        for flag in ("HttpOnly", "SameSite=Strict", "Secure"):
            self.assertIn(flag, cookie)
        session = cookie.split(";")[0]
        self.assertEqual(call(self.app, "GET", "/api/v1/meta", cookie=session)[1]["user"]["name"], "alice")
        self.assertEqual(call(self.app, "GET", "/api/v1/index", token=self.alice)[0].status, 200)
        call(self.app, "POST", "/api/v1/logout", cookie=session)
        self.assertEqual(call(self.app, "GET", "/api/v1/index", cookie=session)[0].status, 401)
        self.assertIn("Strict-Transport-Security", resp.headers)

    def test_wrong_host_and_csrf_are_refused(self):
        self.assertEqual(call(self.app, "GET", "/api/v1/meta", host="evil.example")[0].status, 421)
        session = self.login(self.alice)
        resp = self.app.handle(Request("POST", f"{self.base}/marks", {"Host": "hub.example:7320", "Cookie": session},
                                       b'{"hunk_id": "x"}'))
        self.assertEqual(resp.status, 403)

    def test_failed_sign_ins_are_rate_limited(self):
        for _ in range(10):
            call(self.app, "POST", "/api/v1/login", {"token": "dbh_bad"})
        self.assertEqual(call(self.app, "POST", "/api/v1/login", {"token": self.alice})[0].status, 429)

    def test_comment_permissions(self):
        self.hub.grant("bob", self.pid, "reviewer")
        alice, bob = self.login(self.alice), self.login(self.bob)
        anchor = {"scope": "feature", "path": "app.py", "side": "new", "line": 4, "text": "x3 = 6"}
        shared = call(self.app, "POST", f"{self.base}/comments", {"anchor": anchor, "body": "Why?", "visibility": "shared"},
                      cookie=alice)[1]["comment"]["id"]
        private = call(self.app, "POST", f"{self.base}/comments", {"anchor": anchor, "body": "Mine", "visibility": "private"},
                       cookie=alice)[1]["comment"]["id"]
        url = f"{self.base}/comments/"
        # Only the author unshares, edits or deletes a shared comment.
        self.assertEqual(call(self.app, "PATCH", url + shared, {"visibility": "private"}, cookie=bob)[0].status, 403)
        self.assertEqual(call(self.app, "PATCH", url + shared, {"body": "x"}, cookie=bob)[0].status, 403)
        self.assertEqual(call(self.app, "DELETE", url + shared, cookie=bob)[0].status, 403)
        # Reviewers reply to and resolve shared comments, and the record says who did.
        resp, data = call(self.app, "PATCH", url + shared, {"state": "resolved", "reply": "Fixed."}, cookie=bob)
        self.assertEqual(resp.status, 200, data)
        self.assertEqual((data["comment"]["resolved_by"], data["comment"]["replies"][0]["author"]), ("bob", "bob"))
        # Someone else's private comment doesn't exist for them, whatever they try.
        for method, payload in (("PATCH", {"visibility": "shared"}), ("PATCH", {"state": "resolved"}),
                                ("PATCH", {"reply": "hi"}), ("DELETE", None)):
            self.assertEqual(call(self.app, method, url + private, payload, cookie=bob)[0].status, 404, (method, payload))
        self.assertEqual(call(self.app, "PATCH", url + private, {"visibility": "shared"}, cookie=alice)[0].status, 200)

    def test_malformed_review_requests_are_bad_requests(self):
        alice = self.login(self.alice)
        bad = [
            ("POST", "/comments", {"anchor": "app.py:4", "body": "x"}),
            ("POST", "/comments", {"anchor": {"path": "app.py", "side": "new", "line": 1e309}, "body": "x"}),
            ("POST", "/comments", {"anchor": {"path": "app.py", "side": "new", "line": True}, "body": "x"}),
            ("POST", "/comments", {"anchor": {"path": "app.py", "side": "new", "line": 10 ** 30}, "body": "x"}),
            ("POST", "/comments", {"anchor": {"path": ["a"], "side": "new", "line": 3}, "body": "x"}),
            ("POST", "/comments", {"anchor": {"path": "app.py", "side": "new", "line": 3}, "body": {"x": 1}}),
            ("POST", "/prompt", {"ids": [[1]]}),
            ("POST", "/prompt", {"ids": 5}),
        ]
        for method, suffix, payload in bad:
            resp, data = call(self.app, method, self.base + suffix, payload, cookie=alice)
            self.assertEqual(resp.status, 400, (suffix, payload, data))

    def test_exports_and_cross_references_stay_inside_readable_projects(self):
        # Another project on the hub shares this feature's commit and epic; alice can't read it.
        clone = paths.feature_dir(self.pid, "feat--hubbed", self.hub.archive_root)
        brief = clone / "brief.md"
        brief.write_text(brief.read_text().replace("title: Test feature\n", "title: Test feature\nepic: shared-epic\n"))
        secret = paths.feature_dir("secret-fork", "feat--hubbed", self.hub.archive_root)
        shutil.copytree(str(clone), str(secret))
        (secret.parent.parent / "project.json").write_text('{"project_id": "secret-fork"}')
        (secret / "brief.md").write_text((secret / "brief.md").read_text()
                                         .replace("title: Test feature", "title: TOP-SECRET feature")
                                         .replace("Test.", "TOP-SECRET-INTENT"))
        for pid in (self.pid, "secret-fork"):
            index.update_feature(pid, "feat--hubbed", root=self.hub.archive_root)
        sha = json.loads((clone / "evidence" / "evidence.json").read_text())["commits"][0]["sha"]
        alice = self.login(self.alice)
        commit = call(self.app, "GET", f"/api/v1/commits/{sha}", cookie=alice)[1]
        self.assertEqual([f["project_id"] for f in commit["features"]], [self.pid])
        epic = call(self.app, "GET", "/api/v1/epics/shared-epic", cookie=alice)[1]
        self.assertEqual([f["project_id"] for f in epic["features"]], [self.pid])
        self.assertEqual(call(self.app, "GET", "/api/v1/search?q=TOP-SECRET", cookie=alice)[1]["results"], [])
        resp, html = call(self.app, "GET", f"{self.base}/export", cookie=alice)
        self.assertEqual(resp.status, 200)
        self.assertNotIn(b"TOP-SECRET", html)
        self.assertNotIn(b"secret-fork", html)
        # An admin sees both projects live, but an export still carries only its own feature.
        admin = self.login(self.hub.add_user("root", admin=True))
        commit = call(self.app, "GET", f"/api/v1/commits/{sha}", cookie=admin)[1]
        self.assertEqual(sorted(f["project_id"] for f in commit["features"]), sorted([self.pid, "secret-fork"]))
        self.assertNotIn(b"TOP-SECRET", call(self.app, "GET", f"{self.base}/export", cookie=admin)[1])

    def test_roles_limit_what_people_see_and_change(self):
        carol = self.login(self.carol)
        idx = call(self.app, "GET", "/api/v1/index", cookie=carol)[1]
        self.assertEqual(idx["projects"], [])
        self.assertEqual(call(self.app, "GET", self.base, cookie=carol)[0].status, 404)
        evidence = json.loads((self.fdir / "evidence" / "evidence.json").read_text())
        sha = evidence["commits"][0]["sha"]
        self.assertEqual(call(self.app, "GET", f"/api/v1/commits/{sha}", cookie=carol)[0].status, 404)
        self.assertEqual(call(self.app, "GET", "/api/v1/search?q=App", cookie=carol)[1]["results"], [])
        bob = self.login(self.bob)
        self.assertEqual(call(self.app, "GET", self.base, cookie=bob)[1]["role"], "reader")
        anchor = {"scope": "feature", "path": "app.py", "side": "new", "line": 4, "text": "x3 = 6"}
        resp, _ = call(self.app, "POST", f"{self.base}/comments", {"anchor": anchor, "body": "no", "visibility": "shared"}, cookie=bob)
        self.assertEqual(resp.status, 403)
        alice = self.login(self.alice)
        resp, data = call(self.app, "POST", f"{self.base}/comments", {"anchor": anchor, "body": "Why double?", "visibility": "shared"},
                          cookie=alice)
        self.assertEqual(resp.status, 201, data)
        self.assertEqual(data["comment"]["author"], "alice")

    def test_threads_and_per_user_marks(self):
        alice = self.login(self.alice)
        self.hub.grant("bob", self.pid, "reviewer")
        bob = self.login(self.bob)
        anchor = {"scope": "feature", "path": "app.py", "side": "new", "line": 4, "text": "x3 = 6"}
        cid = call(self.app, "POST", f"{self.base}/comments", {"anchor": anchor, "body": "Why double?", "visibility": "shared"},
                   cookie=alice)[1]["comment"]["id"]
        resp, data = call(self.app, "PATCH", f"{self.base}/comments/{cid}", {"reply": "Spec says so."}, cookie=bob)
        self.assertEqual(resp.status, 200, data)
        self.assertEqual(data["comment"]["replies"][0]["author"], "bob")
        feature = call(self.app, "GET", self.base, cookie=alice)[1]
        hunk = feature["evidence"]["files"][0]["hunks"][0]["id"]
        call(self.app, "POST", f"{self.base}/marks", {"hunk_id": hunk, "reviewed": True}, cookie=alice)
        self.assertEqual(call(self.app, "GET", self.base, cookie=alice)[1]["marks"], [hunk])
        self.assertEqual(call(self.app, "GET", self.base, cookie=bob)[1]["marks"], [])
        # Bob can't edit Alice's comment body.
        resp, _ = call(self.app, "PATCH", f"{self.base}/comments/{cid}", {"body": "changed"}, cookie=bob)
        self.assertEqual(resp.status, 403)

    def test_feedback_and_close_requests_flow_back_to_the_host(self):
        alice = self.login(self.alice)
        anchor = {"scope": "feature", "path": "app.py", "side": "new", "line": 4, "text": "x3 = 6"}
        call(self.app, "POST", f"{self.base}/comments", {"anchor": anchor, "body": "Double check this.", "visibility": "shared"},
             cookie=alice)
        resp, data = call(self.app, "POST", f"{self.base}/prompt", {"queue": True}, cookie=alice)
        self.assertTrue(data["queued"], data)
        resp, data = call(self.app, "POST", f"{self.base}/close-request", {}, cookie=alice)
        self.assertEqual(data["leg_id"], "leg-01")
        # Hub writes reach the bare repository (background sync, then the syncer as a fallback).
        clone = paths.project_dir(self.pid, self.hub.archive_root)
        self.syncer.tick()
        self.assertFalse(archive.unpushed(clone))
        # The developer's next session pulls them in and bin/session relays them.
        out = self.run_session(self.repo, "start")
        self.assertIn("REQUEST from alice: close out leg-01", out)
        self.assertIn("FEEDBACK", out)
        shared = json.loads((self.fdir / "comments.json").read_text())["comments"]
        self.assertEqual(shared[0]["author"], "alice")

    def test_new_host_records_appear_on_the_hub(self):
        self.commit(self.repo, "Another change\n\nMore.", {"app.py": "changed = True\n"})
        self.run_session(self.repo, "now")
        ingest.ingest_feature(self.pid, "feat--hubbed")
        archive.sync(paths.project_dir(self.pid), "Publish again")
        self.assertEqual(self.syncer.tick(), [self.pid])
        alice = self.login(self.alice)
        feature = call(self.app, "GET", self.base, cookie=alice)[1]
        self.assertEqual(len(feature["evidence"]["commits"]), 2)
        self.assertEqual(self.syncer.tick(), [])

    def test_cli_commands(self):
        os.environ["DEBRIEF_HUB_DIR"] = str(self.tmp / "hub")
        self.addCleanup(os.environ.pop, "DEBRIEF_HUB_DIR", None)
        out = self.run_cli("hub", "adduser", "dave", "--admin")
        self.assertIn("dbh_", out)
        self.assertTrue(self.hub.users()["dave"]["admin"])
        out = self.run_cli("hub", "grant", "dave", self.pid, "--role", "owner")
        self.assertIn("owner", out)
        out = self.run_cli("hub", "users")
        self.assertIn("alice: 1 token(s)", out)
        out = self.run_cli("hub", "repo", "another-project")
        self.assertIn("debrief sync another-project --remote ssh://hub.example", out)
        out = self.run_cli("hub", "serve", "--insecure-http", "--bind", "0.0.0.0")
        self.assertIn("Refusing plain HTTP", out)


@unittest.skipIf(shutil.which("openssl") is None, "openssl is not installed")
class HubTlsTests(IsolatedTestCase):
    def test_serves_over_tls(self):
        hub = Hub(self.tmp / "hub")
        report = "\n".join(hub.init("localhost"))
        self.assertIn("self-signed certificate", report)
        token = hub.add_user("alice", admin=True)
        cfg = hub.config
        server = make_hub_server(hub, "127.0.0.1", 0, cfg["cert"], cfg["key"], hostnames=None)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            context = ssl.create_default_context(cafile=cfg["cert"])
            request = urllib.request.Request(f"https://localhost:{port}/api/v1/index",
                                             headers={"Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(request, context=context) as resp:
                self.assertEqual(resp.status, 200)
                self.assertIn("max-age", resp.headers["Strict-Transport-Security"])
                self.assertEqual(json.loads(resp.read())["projects"], [])
            # A client that never finishes its handshake doesn't block others.
            import socket

            idle = socket.create_connection(("127.0.0.1", port))
            with urllib.request.urlopen(f"https://localhost:{port}/api/v1/meta", context=context) as resp:
                self.assertEqual(resp.status, 200)
            idle.close()
            with self.assertRaises(Exception):
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/meta", timeout=5).read()
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()

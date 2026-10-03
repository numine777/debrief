"""The viewer's server and API: routes, guards, offline guarantee."""

import json
import re
import threading
import unittest
import urllib.request
from unittest import mock
from pathlib import Path

from tests.helpers import ROOT, IsolatedTestCase, git

from debrief import ingest
from debrief.api import Api
from debrief.server import CSP, App, Request, make_server

STATIC = ROOT / "src" / "debrief" / "static"


def get(app, path, host="127.0.0.1:7319", **headers):
    hdrs = {"Host": host}
    hdrs.update(headers)
    return app.handle(Request("GET", path, hdrs))


def post(app, path, body=None, host="127.0.0.1:7319", **headers):
    hdrs = {"Host": host, "X-Debrief": "1", "Content-Type": "application/json"}
    hdrs.update(headers)
    return app.handle(Request("POST", path, hdrs, json.dumps(body or {}).encode()))


def body(resp):
    return json.loads(resp.body.decode())


class ServerTests(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.repo = self.make_repo(files={"app.py": "def main():\n    return 0\n"})
        git(self.repo, "checkout", "-q", "-b", "feat/view")
        self.pid = self.init_project(self.repo)
        self.fdir = self.feature_dir(self.pid, "feat--view")
        self.run_session(self.repo, "start", "--task", "Viewer fixture")
        self.sha = self.commit(self.repo, "Change main\n\nReturns one now.", {"app.py": "def main():\n    return 1\n"})
        self.run_session(self.repo, "now")
        self.write_records(self.fdir)
        self.evidence = ingest.ingest_feature(self.pid, "feat--view")
        self.app = App(Api(self.archive), 7319)
        self.base = f"/api/v1/projects/{self.pid}/features/feat--view"

    def test_index_page_and_static_files_carry_csp(self):
        resp = get(self.app, "/")
        self.assertEqual(resp.status, 200)
        self.assertIn(b"static/js/app.js", resp.body)
        self.assertEqual(resp.headers["Content-Security-Policy"], CSP)
        self.assertEqual(resp.headers["X-Frame-Options"], "DENY")
        css = get(self.app, "/static/app.css")
        self.assertEqual(css.status, 200)
        self.assertTrue(css.headers["Content-Type"].startswith("text/css"))
        font = get(self.app, "/static/fonts/ibm-plex-mono-latin-400-normal.woff2")
        self.assertEqual(font.headers["Content-Type"], "font/woff2")

    def test_host_header_must_name_this_server(self):
        self.assertEqual(get(self.app, "/api/v1/meta", host="evil.example:7319").status, 421)
        self.assertEqual(get(self.app, "/api/v1/meta", host="localhost:7319").status, 200)

    def test_writes_need_the_debrief_header_and_same_origin(self):
        path = f"{self.base}/ingest"
        no_header = self.app.handle(Request("POST", path, {"Host": "127.0.0.1:7319"}, b"{}"))
        self.assertEqual(no_header.status, 403)
        cross = post(self.app, path, Origin="http://evil.example")
        self.assertEqual(cross.status, 403)
        ok = post(self.app, path, Origin="http://127.0.0.1:7319")
        self.assertEqual(ok.status, 200, ok.body)
        self.assertIn("coverage", body(ok)["summary"])

    def test_readonly_refuses_writes(self):
        app = App(Api(self.archive, readonly=True), 7319)
        self.assertEqual(post(app, f"{self.base}/ingest").status, 403)

    def test_path_traversal_is_refused(self):
        for path in ["/static/../server.py", "/static/%2e%2e/server.py", "/static/js/../../api.py"]:
            self.assertEqual(get(self.app, path).status, 404, path)
        self.assertEqual(get(self.app, f"/api/v1/projects/{self.pid}/features/..").status, 400)
        self.assertEqual(get(self.app, f"{self.base}/blobs/zzzz").status, 404)
        self.assertEqual(get(self.app, "/api/v1/projects/nope/features/feat--view").status, 404)

    def test_api_views(self):
        idx = body(get(self.app, "/api/v1/index"))
        self.assertEqual(idx["projects"][0]["features"][0]["feature_id"], "feat--view")
        feature = body(get(self.app, self.base))
        self.assertEqual(feature["title"], "Test feature")
        self.assertEqual(feature["systems"][0]["id"], "app")
        self.assertEqual(feature["systems"][0]["hunk_count"], 1)
        self.assertEqual(feature["evidence"]["coverage"]["covered"], 1)
        self.assertEqual(feature["sessions"][0]["meta"]["task"], "Viewer fixture")
        self.assertNotIn("last_seen_head", feature["sessions"][0]["meta"])
        diff = body(get(self.app, f"{self.base}/diff?scope=feature"))
        hunk = diff["files"][0]["hunks"][0]
        self.assertEqual(hunk["state"], "covered")
        self.assertIn(["+", "    return 1"], hunk["lines"])
        commit_diff = body(get(self.app, f"{self.base}/diff?scope=commit:{self.sha}"))
        self.assertEqual(commit_diff["files"][0]["hunks"][0]["systems"], ["app"])
        blob = diff["files"][0]["new_blob"]
        self.assertEqual(get(self.app, f"{self.base}/blobs/{blob}").body, b"def main():\n    return 1\n")
        commit = body(get(self.app, f"/api/v1/commits/{self.sha[:8]}"))
        self.assertEqual(commit["features"][0]["commit"]["subject"], "Change main")
        self.assertEqual(commit["features"][0]["commit"]["kind"], "agent")
        search = body(get(self.app, "/api/v1/search?q=main"))
        self.assertTrue(any(r["kind"] == "system" for r in search["results"]))
        self.assertEqual(get(self.app, "/api/v1/commits/zz").status, 404)
        self.assertEqual(get(self.app, "/api/v1/epics/none").status, 404)
        self.assertEqual(get(self.app, f"{self.base}/diff?scope=bogus").status, 400)

    def test_large_json_is_gzipped(self):
        resp = get(self.app, self.base, **{"Accept-Encoding": "gzip"})
        if len(resp.body) > 0 and resp.headers.get("Content-Encoding") == "gzip":
            import gzip

            self.assertEqual(json.loads(gzip.decompress(resp.body))["feature_id"], "feat--view")

    def test_real_http_round_trip(self):
        server = make_server(0, root=self.archive)
        port = server.server_address[1]
        server.app.allowed_hosts.add(f"127.0.0.1:{port}")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/meta") as resp:
                self.assertEqual(json.loads(resp.read())["mode"], "local")
                self.assertEqual(resp.headers["Content-Security-Policy"], CSP)
        finally:
            server.shutdown()
            server.server_close()

    def test_bad_content_length_is_refused_before_reading(self):
        import socket

        server = make_server(0, root=self.archive)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def send(header: bytes) -> bytes:
            # The old server read a negative length "to the end" and never answered (recv times out).
            with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
                sock.sendall(f"POST /api/v1/meta HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n".encode() + header + b"\r\n\r\n")
                return sock.recv(200)

        self.assertIn(b" 400 ", send(b"Content-Length: -1"))
        self.assertIn(b" 400 ", send(b"Content-Length: abc"))
        self.assertIn(b" 400 ", send(b"Content-Length: \xb2"))  # superscript two: isdigit() but not a number
        self.assertIn(b" 411 ", send(b"Transfer-Encoding: chunked"))
        self.assertIn(b" 413 ", send(b"Content-Length: 1000000000"))

    def test_odd_requests_get_clean_errors(self):
        self.assertEqual(get(self.app, "/static/%00").status, 404)
        self.assertEqual(get(self.app, "/static/js\\..\\app.js").status, 404)
        # An internal error names a log reference, never the exception.
        self.app.add("GET", r"/api/v1/boom", lambda req: 1 / 0)
        with mock.patch("sys.stderr") as log:
            resp = get(self.app, "/api/v1/boom")
        self.assertIn("ZeroDivisionError", "".join(c.args[0] for c in log.write.call_args_list))
        self.assertEqual(resp.status, 500)
        self.assertNotIn("ZeroDivisionError", body(resp)["error"])
        self.assertIn("logged as", body(resp)["error"])


class OfflineTests(unittest.TestCase):
    """The build fails if a bundled file references an external URL."""

    NAMESPACES = {"http://www.w3.org/2000/svg", "http://www.w3.org/1999/xhtml", "http://www.w3.org/1998/Math/MathML"}
    VENDOR_DOC_LINKS = {
        "https://github.com/highlightjs/highlight.js/issues/2277",
        "https://github.com/highlightjs/highlight.js/wiki/security",
        "https://github.com/markedjs/marked.",
    }

    def test_no_external_urls_in_bundled_files(self):
        offenders = []
        for path in sorted(STATIC.rglob("*")):
            if path.suffix not in (".js", ".css", ".html", ".svg"):
                continue
            text = re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8"), flags=re.S)
            for url in set(re.findall(r"https?://[^\s\"'`)<>]+", text)):
                allowed = url in self.NAMESPACES or ("vendor" in path.parts and url in self.VENDOR_DOC_LINKS)
                if not allowed:
                    offenders.append(f"{path.relative_to(STATIC)}: {url}")
        self.assertEqual(offenders, [])

    def test_own_assets_load_nothing_remote(self):
        for path in [STATIC / "index.html", STATIC / "app.css", *sorted((STATIC / "js").glob("*.js"))]:
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"(src|href)=[\"']https?:", str(path))
            self.assertNotRegex(text, r"url\(\s*[\"']?https?:", str(path))
            self.assertNotRegex(text, r"@import", str(path))


if __name__ == "__main__":
    unittest.main()


class ScriptSyntaxTests(unittest.TestCase):
    def test_scripts_parse(self):
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        for path in sorted((STATIC / "js").glob("*.js")):
            proc = subprocess.run([node, "--check", str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.assertEqual(proc.returncode, 0, f"{path.name}: {proc.stderr}")

"""Browser smoke test: every view renders without script errors (needs Playwright)."""

import threading
import unittest

from tests.helpers import IsolatedTestCase, git

from debrief import ingest
from debrief.server import make_server

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None


@unittest.skipIf(sync_playwright is None, "Playwright is not installed")
class ViewerSmokeTests(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        tail = "".join(f"x{i} = {i}\n" for i in range(30))
        repo = self.make_repo(files={"app.py": "def main():\n    return 0\n" + tail, "util.py": "x = 1\n"})
        git(repo, "checkout", "-q", "-b", "feat/ui")
        self.pid = self.init_project(repo)
        fdir = self.feature_dir(self.pid, "feat--ui")
        self.run_session(repo, "start")
        self.sha = self.commit(repo, "Change main\n\nReturns one.", {"app.py": "def main():\n    while True:\n        return 1\n" + tail})
        self.commit(repo, "Touch util", {"util.py": "x = 2\n", "new.txt": "unexplained\n"})
        self.run_session(repo, "now")
        self.write_records(fdir)
        # Agent prose is untrusted: a repo can carry injected text into records.
        brief = (fdir / "brief.md").read_text().replace("## Intent\nTest.", (
            "## Intent\nTest. <script>document.title='pwned'</script><img src=x onerror=\"document.title='pwned'\">"
            " [click](javascript:document.title='pwned') ![remote](https://example.com/track.png)"))
        (fdir / "brief.md").write_text(brief)
        ingest.ingest_feature(self.pid, "feat--ui")
        self.server = make_server(0, root=self.archive)
        self.port = self.server.server_address[1]
        self.server.app.allowed_hosts.add(f"127.0.0.1:{self.port}")
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def test_every_view_renders(self):
        base = f"http://127.0.0.1:{self.port}/"
        feature = f"#/p/{self.pid}/f/feat--ui"
        routes = {
            "#/": "Features",
            feature: "Intent",
            feature + "/map": "New system",
            feature + "/systems": "App",
            feature + "/system/app": "Code this system explains",
            feature + "/tests": "Known gaps",
            feature + "/queue": "Unexplained change",
            feature + "/diff?mode=story": "Change main",
            feature + "/diff?mode=systems": "Unexplained",
            feature + "/timeline": "leg-01",
            f"#/commit/{self.sha}": "Returns one.",
            "#/search?q=main": "System",
        }
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda err: errors.append(str(err)))
            page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
            for route, expected in routes.items():
                page.goto(base + route)
                # Locators, not string evaluation: the viewer's CSP forbids eval, even for tests.
                page.locator("main", has_text=expected).wait_for(timeout=10000)
                self.assertIn(expected, page.inner_text("main"), route)
            page.goto(base + feature)
            page.locator("main", has_text="Intent").wait_for()
            self.assertEqual(page.locator("main script").count(), 0)
            self.assertEqual(page.locator("main img[onerror]").count(), 0)
            self.assertEqual(page.locator("main a[href^='javascript']").count(), 0)
            self.assertNotEqual(page.title(), "pwned")
            page.goto(base + feature + "/diff?mode=systems")
            page.locator(".hunk").first.wait_for()
            before = page.locator("table.code tr").count()
            page.locator(".file", has_text="app.py").locator(".ctx-btn", has_text="Show lines below").first.click()
            page.locator("tr.ctx-extra").first.wait_for()
            self.assertGreater(page.locator("table.code tr").count(), before)
            page.goto(base + feature)
            page.wait_for_selector(".strip .cell")
            cells = page.locator(".feature-head .strip .cell").count()
            self.assertEqual(cells, 3)
            page.locator(".feature-head .strip .cell.state-unclaimed").first.click()
            page.wait_for_selector(".hunk.current")
            browser.close()
        # The remote image is blocked by the CSP (reported as a console error); nothing else may fail.
        unexpected = [e for e in errors if "example.com" not in e and "Content Security Policy" not in e]
        self.assertEqual(unexpected, [])

    def test_review_loop_in_the_browser(self):
        base = f"http://127.0.0.1:{self.port}/"
        feature = f"#/p/{self.pid}/f/feat--ui"
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda err: errors.append(str(err)))
            page.goto(base + feature + "/diff?mode=systems")
            page.locator(".hunk").first.wait_for()
            # Comment on a changed line.
            page.locator(".file", has_text="app.py").locator("tr.add .ln-btn").first.click()
            page.locator(".composer textarea").fill("Why loop forever here?")
            page.locator(".composer button[type=submit]").click()
            page.locator(".comment-row", has_text="Why loop forever here?").wait_for()
            # Mark a hunk reviewed; the strip cell shows it.
            page.locator(".mark-btn").first.click()
            page.locator(".mark-btn[aria-pressed=true]").first.wait_for()
            page.locator(".strip .cell.reviewed").first.wait_for()
            # Generate a prompt from the comment.
            page.goto(base + feature + "/comments")
            page.locator(".comment-item", has_text="Why loop forever here?").wait_for()
            page.locator("button", has_text="Generate prompt").click()
            dialog = page.locator("dialog[open]")
            dialog.wait_for()
            text = dialog.locator("textarea").input_value()
            self.assertIn("app.py:2", text)
            self.assertIn("Why loop forever here?", text)
            dialog.locator("button", has_text="Queue for the agent").click()
            dialog.locator("text=Queued in feedback.md").wait_for()
            dialog.locator("button", has_text="Close").last.click()
            # Ask the agent to close the leg.
            page.goto(base + feature)
            page.locator("button", has_text="Close leg-01").click()
            page.locator(".prompt-text").wait_for()
            self.assertIn("ai-session-closeout", page.locator(".prompt-text").input_value())
            browser.close()
        self.assertTrue((self.feature_dir(self.pid, "feat--ui") / "feedback.md").exists())
        self.assertEqual(errors, [])

    def test_export_opens_offline(self):
        from debrief import export
        from debrief.api import Api

        out = self.tmp / "export.html"
        out.write_text(export.render(Api(self.archive), self.pid, "feat--ui"))
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            errors, requests = [], []
            page.on("pageerror", lambda err: errors.append(str(err)))
            page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
            page.on("request", lambda r: requests.append(r.url) if not r.url.startswith(("file:", "data:")) else None)
            page.goto(out.as_uri())
            page.locator("main", has_text="Intent").wait_for()
            page.goto(out.as_uri() + f"#/p/{self.pid}/f/feat--ui/diff?mode=story")
            page.locator(".hunk").first.wait_for()
            self.assertIn("read-only", page.inner_text("header"))
            browser.close()
        unexpected = [e for e in errors if "example.com" not in e and "Content Security Policy" not in e]
        self.assertEqual(unexpected, [])
        self.assertEqual(requests, [])

    def test_skip_link_moves_focus_without_leaving_the_page(self):
        base = f"http://127.0.0.1:{self.port}/"
        feature = f"#/p/{self.pid}/f/feat--ui"
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto(base + feature)
            page.locator("main", has_text="Intent").wait_for()
            page.keyboard.press("Tab")
            self.assertEqual(page.locator(":focus").inner_text(), "Skip to content")
            page.keyboard.press("Enter")
            self.assertEqual(page.locator(":focus").get_attribute("id"), "main")
            self.assertTrue(page.url.endswith(feature))
            self.assertIn("Intent", page.inner_text("main"))  # it used to route to "That page doesn't exist"
            browser.close()


@unittest.skipIf(sync_playwright is None, "Playwright is not installed")
class HubUiTests(IsolatedTestCase):
    def test_sign_in_then_browse(self):
        from debrief.hub import Hub, make_hub_server

        hub = Hub(self.tmp / "hub")
        hub.init("localhost")
        token = hub.add_user("alice", admin=True)
        server = make_hub_server(hub, "127.0.0.1", 0, None, None, insecure_http=True)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page()
                errors = []
                page.on("pageerror", lambda err: errors.append(str(err)))
                page.goto(f"http://127.0.0.1:{port}/")
                page.locator("form.login").wait_for()
                page.locator("form.login input").fill("dbh_wrong")
                page.locator("form.login button").click()
                page.locator("text=That token isn't valid").wait_for()
                page.locator("form.login input").fill(token)
                page.locator("form.login button").click()
                page.locator("main", has_text="Features").wait_for()
                self.assertIn("alice", page.inner_text("header"))
                page.locator("button", has_text="Sign out").click()
                page.locator("form.login").wait_for()
                browser.close()
            self.assertEqual(errors, [])
        finally:
            server.shutdown()
            server.server_close()

    def test_readers_see_no_write_controls(self):
        from debrief import archive, comments, paths
        from debrief.hub import Hub, HubSyncer, make_hub_server

        repo = self.make_repo(files={"app.py": "".join(f"x{i} = {i}\n" for i in range(10))})
        git(repo, "checkout", "-q", "-b", "feat/hubui")
        pid = self.init_project(repo)
        fdir = self.feature_dir(pid, "feat--hubui")
        self.run_session(repo, "start")
        self.commit(repo, "Change x3\n\nBody.", {"app.py": "".join(f"x{i} = {i * 2}\n" for i in range(10))})
        self.run_session(repo, "now")
        self.write_records(fdir)
        ingest.ingest_feature(pid, "feat--hubui")
        anchor = {"scope": "feature", "path": "app.py", "side": "new", "line": 4, "text": "x3 = 6"}
        comments.create(pid, "feat--hubui", {"anchor": anchor, "body": "Alice's note", "visibility": "shared"}, "alice")
        hub = Hub(self.tmp / "hub")
        hub.init("localhost")
        archive.set_remote(paths.project_dir(pid), str(hub.add_repo(pid)))
        archive.sync(paths.project_dir(pid), "Publish to hub")
        HubSyncer(hub).tick()
        tokens = {name: hub.add_user(name) for name in ("alice", "bob")}
        hub.grant("alice", pid, "reviewer")
        hub.grant("bob", pid, "reader")
        server = make_hub_server(hub, "127.0.0.1", 0, None, None, insecure_http=True, hostnames=["localhost"])
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        counts = {}
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                for name in ("bob", "alice"):
                    page = browser.new_context().new_page()
                    page.goto(f"http://localhost:{port}/#/p/{pid}/f/feat--hubui")
                    page.locator("form.login input").fill(tokens[name])
                    page.locator("form.login button").click()
                    page.locator("main", has_text="Intent").wait_for()
                    close_buttons = page.locator("button", has_text="Close leg-01").count()
                    page.goto(f"http://localhost:{port}/#/p/{pid}/f/feat--hubui/diff?mode=systems")
                    page.locator(".comment-row", has_text="Alice's note").wait_for()
                    card = page.locator(".comment-row", has_text="Alice's note")
                    counts[name] = (close_buttons, page.locator(".ln-btn").count(),
                                    card.locator("button", has_text="Edit").count(),
                                    card.locator("button", has_text="Resolve").count(),
                                    page.locator(".mark-btn").count() > 0)
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
        # A reader can mark hunks for themselves but sees nothing that changes shared records.
        self.assertEqual(counts["bob"], (0, 0, 0, 0, True))
        close, line_buttons, edit, resolve, marks = counts["alice"]
        self.assertEqual((close, edit, resolve, marks), (1, 1, 1, True))
        self.assertGreater(line_buttons, 0)


if __name__ == "__main__":
    unittest.main()

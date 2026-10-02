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


if __name__ == "__main__":
    unittest.main()

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


PHONE = {"viewport": {"width": 390, "height": 844}, "is_mobile": True, "has_touch": True, "device_scale_factor": 2}
DROP_Y = "Drop y from util because nothing reads it any longer anywhere in the app"

# Everything on the page that sticks out past the right edge of the screen, other than inside something
# that scrolls or clips within the screen. Code is skipped: a wrapped line's trailing spaces may hang.
# The width is the layout's (clientWidth): a phone widens innerWidth to fit content that overflows.
OFFSCREEN = """() => {
  const vw = document.documentElement.clientWidth, out = [];
  for (const el of document.querySelectorAll("body *")) {
    if (el.closest("td.src")) continue;
    const r = el.getBoundingClientRect();
    if (!r.width || r.right <= vw + 1) continue;
    let held = false;
    for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
      const o = getComputedStyle(p).overflowX;
      if (o !== "visible" && p.getBoundingClientRect().right <= vw + 1) { held = true; break; }
    }
    if (!held) out.push(el.tagName.toLowerCase() + "." + (el.getAttribute("class") || ""));
  }
  return out;
}"""

PHONE_TESTS_YAML = """tests:
  - id: app-tests
    validates: [app]
    claim: "Main returns one after computing the answer, whatever the four arguments are."
    kind: unit
    anchors: []
    command: "python3 -c 'print(1)'"
    claimed_result: pass
gaps: []
"""


@unittest.skipIf(sync_playwright is None, "Playwright is not installed")
class PhoneLayoutTests(IsolatedTestCase):
    """The viewer on a phone-sized touch screen."""

    def setUp(self):
        super().setUp()
        tail = "".join(f"x{i} = {i}\n" for i in range(30))
        repo = self.make_repo(files={"app.py": "def main():\n    return 0\n" + tail, "util.py": "x = 1\ny = 2\n"})
        git(repo, "checkout", "-q", "-b", "feat/phone")
        self.pid = self.init_project(repo)
        fdir = self.feature_dir(self.pid, "feat--phone")
        self.run_session(repo, "start")
        long_line = "        answer = compute_the_answer(first_argument, second_argument, third_argument, fourth)\n"
        self.commit(repo, "Change main\n\nReturns one.", {"app.py": "def main():\n    if True:\n" + long_line + "        return 1\n" + tail})
        self.commit(repo, f"{DROP_Y}\n\nNothing reads it.\n\nReviewed-At: https://example.invalid/reviews/{'0123456789' * 6}",
                    {"util.py": "x = 1\n"})
        self.run_session(repo, "now")
        self.write_records(fdir, tests_yaml=PHONE_TESTS_YAML)
        brief = (fdir / "brief.md").read_text()
        (fdir / "brief.md").write_text(brief.replace("review_first: []", 'review_first:\n  - {target: app, why: "Main changed"}'))
        for sid, title, path, deps in (("app", "App", "app.py", "[{system: util, relation: reads x}]"), ("util", "Util", "util.py", "[]")):
            self.write(fdir / "systems" / f"{sid}.md", (
                f"---\nid: {sid}\ntitle: {title}\nchange: new\ndepends_on: {deps}\n"
                f"anchors:\n  - {{path: {path}, role: core}}\ncritical_paths: []\ndecisions: []\n---\n\n"
                "## Purpose\nP.\n\n## Change\nNew.\n\n## How it works\nH.\n\n## Limitations\nL.\n"))
        ingest.ingest_feature(self.pid, "feat--phone")
        self.server = make_server(0, root=self.archive)
        self.port = self.server.server_address[1]
        self.server.app.allowed_hosts.add(f"127.0.0.1:{self.port}")
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.port}/"
        self.feature = f"#/p/{self.pid}/f/feat--phone"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def open(self, p, route, wait, **context):
        page = p.chromium.launch().new_context(**dict(PHONE, **context)).new_page()
        page.errors = []
        page.on("pageerror", lambda err: page.errors.append(str(err)))
        page.on("console", lambda msg: page.errors.append(msg.text) if msg.type == "error" else None)
        page.goto(self.base + route)
        page.locator(wait).first.wait_for()
        return page

    def test_nothing_runs_off_a_phone_screen(self):
        sha = git(self.tmp / "repo", "rev-parse", "HEAD")
        routes = ["#/", self.feature, self.feature + "/map", self.feature + "/systems", self.feature + "/system/app",
                  self.feature + "/tests", self.feature + "/queue", self.feature + "/diff?mode=story",
                  self.feature + "/diff?mode=systems", self.feature + "/timeline", self.feature + "/comments",
                  f"#/commit/{sha}", "#/search?q=main"]
        with sync_playwright() as p:
            for width in (360, 390):
                page = self.open(p, "#/", "main h1", viewport={"width": width, "height": 800})
                for route in routes:
                    page.goto(self.base + route)
                    page.locator("main :is(h1, h3, .feature-head)").first.wait_for()
                    page.wait_for_timeout(300)  # diffs and tab scrolling settle after the first paint
                    self.assertLessEqual(page.evaluate("() => document.documentElement.scrollWidth"), width, route)
                    self.assertEqual(page.evaluate(OFFSCREEN), [], f"{route} at {width} px")
                # A long-running feature: a dozen legs and a long branch name still fit.
                page.goto(self.base + self.feature)
                page.locator(".feature-head .legs").wait_for()
                page.evaluate("""() => {
                  const legs = document.querySelector(".feature-head .legs");
                  for (let i = 2; i <= 13; i++) { const a = legs.firstChild.cloneNode(true); a.textContent = "Leg " + i; legs.appendChild(a); }
                  document.querySelector(".feature-head .branch").textContent = "feat/PROJ-1234-a-very-long-branch-name-for-the-phone-layout";
                }""")
                self.assertLessEqual(page.evaluate("() => document.documentElement.scrollWidth"), width)
                self.assertEqual(page.evaluate(OFFSCREEN), [], f"long header at {width} px")
                # ... and so do long anchor paths beside a system's prose.
                page.goto(self.base + self.feature + "/system/app")
                page.locator(".anchor-list li").first.wait_for()
                page.evaluate("""() => {
                  const span = document.querySelector(".anchor-list li .mono");
                  span.textContent = "src/a/deeply/nested/package/module_with_a_long_name.py:Owner.method_with_a_long_name (120-180)";
                }""")
                self.assertLessEqual(page.evaluate("() => document.documentElement.scrollWidth"), width)
                self.assertEqual(page.evaluate(OFFSCREEN), [], f"long anchors at {width} px")
                self.assertEqual(page.errors, [])

    def test_code_rows_show_one_line_number_and_hang_wrapped_lines(self):
        with sync_playwright() as p:
            page = self.open(p, self.feature + "/diff?mode=systems", ".hunk")
            long_line = page.locator("tr.add", has_text="compute_the_answer")
            self.assertEqual(long_line.locator("td.ln > span:visible").count(), 1)
            self.assertEqual(long_line.locator("td.ln > span:visible").inner_text(), "3")
            removed = page.locator(".file", has_text="util.py").locator("tr.del")
            self.assertEqual(removed.locator("td.ln > span:visible").inner_text(), "2")
            unchanged = page.locator(".file", has_text="app.py").locator("tr.ctx").first
            self.assertEqual(unchanged.locator("td.ln > span:visible").count(), 1)
            # The long line wraps, and its continuation hangs two columns past its eight spaces.
            src = long_line.locator("td.src")
            self.assertEqual(src.evaluate("el => el.style.getPropertyValue('--hang')"), "10ch")
            self.assertGreater(src.bounding_box()["height"], 40)
            # Wider screens keep both columns.
            wide = self.open(p, self.feature + "/diff?mode=systems", ".hunk", viewport={"width": 1280, "height": 900},
                             is_mobile=False, has_touch=False)
            row = wide.locator(".file", has_text="app.py").locator("tr.ctx").first
            self.assertEqual(row.locator("td.ln > span:visible").count(), 2)
            self.assertEqual(page.errors + wide.errors, [])

    def test_tabs_stay_in_reach(self):
        with sync_playwright() as p:
            page = self.open(p, self.feature + "/diff?mode=systems", ".hunk", viewport={"width": 390, "height": 480})
            current = page.locator(".tabs a[aria-current=page]")
            self.assertEqual(current.inner_text().strip(), "Diff")
            page.wait_for_timeout(100)  # the bar scrolls the current view into sight after layout
            box = current.bounding_box()
            self.assertGreaterEqual(box["x"], 0)
            self.assertLessEqual(box["x"] + box["width"], 390)
            # Off the Brief the header is compact; the tab bar, not the top bar, stays on screen.
            self.assertFalse(page.locator(".feature-head .facts").is_visible())
            page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(100)
            self.assertEqual(page.locator(".tabs-bar").bounding_box()["y"], 0)
            self.assertLess(page.locator(".topbar").bounding_box()["y"], 0)
            # The brief keeps the full header.
            page.goto(self.base + self.feature)
            page.locator("main", has_text="Intent").wait_for()
            self.assertTrue(page.locator(".feature-head .facts").is_visible())
            # Desktop keeps its sticky top bar and lets the tabs scroll away with the header.
            wide = self.open(p, self.feature + "/diff?mode=systems", ".hunk", viewport={"width": 1280, "height": 480},
                             is_mobile=False, has_touch=False)
            wide.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            wide.wait_for_timeout(100)
            self.assertEqual(wide.locator(".topbar").bounding_box()["y"], 0)
            self.assertLess(wide.locator(".tabs-bar").bounding_box()["y"], 0)
            self.assertEqual(page.errors + wide.errors, [])

    def test_tables_become_labelled_cards(self):
        with sync_playwright() as p:
            page = self.open(p, self.feature + "/tests", "table.tests-table")
            self.assertFalse(page.locator("table.tests-table thead").is_visible())
            claim = page.locator("table.tests-table td.claim").first
            self.assertGreater(claim.bounding_box()["width"], 300)  # not squeezed into a column
            self.assertEqual(page.locator("table.tests-table td.command").get_attribute("data-label"), "Command")
            # The rows keep their table semantics once CSS stops laying them out as a table.
            self.assertEqual(page.get_by_role("row").count(), 1)
            self.assertEqual(page.get_by_role("cell").count(), 6)
            page.goto(self.base + self.feature + "/systems")
            page.locator("table.systems-table").wait_for()
            self.assertFalse(page.locator("table.systems-table thead").is_visible())
            self.assertEqual(page.locator("table.systems-table tbody tr").count(), 2)
            wide = self.open(p, self.feature + "/tests", "table.tests-table", viewport={"width": 1280, "height": 800},
                             is_mobile=False, has_touch=False)
            self.assertTrue(wide.locator("table.tests-table thead").is_visible())
            self.assertEqual(page.errors + wide.errors, [])

    def test_map_becomes_a_layered_list(self):
        with sync_playwright() as p:
            page = self.open(p, self.feature + "/map", ".map-legend")
            self.assertFalse(page.locator(".map-wrap").is_visible())
            cards = page.locator(".map-list .map-card")
            self.assertEqual(cards.count(), 2)
            # App depends on Util, so App's layer comes first and its card names the dependency.
            self.assertIn("App", cards.nth(0).inner_text())
            self.assertIn("reads x", cards.nth(0).locator(".deps").inner_text())
            cards.nth(0).locator(".deps a", has_text="util").click()
            page.locator("h2.system-title", has_text="Util").wait_for()
            wide = self.open(p, self.feature + "/map", ".map-legend", viewport={"width": 1280, "height": 800},
                             is_mobile=False, has_touch=False)
            self.assertTrue(wide.locator(".map-wrap svg").is_visible())
            self.assertFalse(wide.locator(".map-list").is_visible())
            self.assertEqual(page.errors + wide.errors, [])

    def test_narrow_screens_lead_with_what_to_review(self):
        with sync_playwright() as p:
            page = self.open(p, self.feature, ".feature-head")
            targets = page.locator(".review-first:visible")
            self.assertEqual(targets.count(), 1)
            self.assertLess(targets.bounding_box()["y"], page.locator("h2", has_text="Intent").bounding_box()["y"])
            # A system's connections come before its code once the columns stack.
            page.goto(self.base + self.feature + "/system/app")
            page.locator(".sys-code .hunk").first.wait_for()
            self.assertLess(page.locator(".system-page > aside").bounding_box()["y"], page.locator(".sys-code").bounding_box()["y"])
            wide = self.open(p, self.feature + "/system/app", ".sys-code .hunk", viewport={"width": 1280, "height": 800},
                             is_mobile=False, has_touch=False)
            aside, code = wide.locator(".system-page > aside").bounding_box(), wide.locator(".sys-code").bounding_box()
            self.assertLess(code["x"] + code["width"], aside["x"])  # code stays in the left column
            wide.goto(self.base + self.feature)
            wide.locator(".feature-head").wait_for()
            self.assertEqual(wide.locator("aside .review-first:visible").count(), 1)
            self.assertEqual(wide.locator(".review-first:visible").count(), 1)
            self.assertEqual(page.errors + wide.errors, [])

    def test_jump_menu_stands_in_for_the_outline(self):
        with sync_playwright() as p:
            page = self.open(p, self.feature + "/diff?mode=story", ".card.commit", viewport={"width": 390, "height": 420})
            self.assertFalse(page.locator(".outline").is_visible())
            jump = page.locator("select.jump")
            self.assertEqual(jump.locator("option").all_inner_texts(), ["Jump to a commit", "1. Change main", "2. " + DROP_Y])
            jump.select_option(index=2)
            page.wait_for_timeout(200)
            card = page.locator(".card.commit", has_text="Drop y from util")
            self.assertLess(abs(card.bounding_box()["y"] - 72), 40)  # under the sticky tabs, where scroll-margin puts it
            self.assertEqual(jump.input_value(), "")  # ready for the next jump
            wide = self.open(p, self.feature + "/diff?mode=story", ".card.commit", viewport={"width": 1280, "height": 800},
                             is_mobile=False, has_touch=False)
            self.assertTrue(wide.locator(".outline").is_visible())
            self.assertFalse(wide.locator("select.jump").is_visible())
            self.assertEqual(page.errors + wide.errors, [])

    def test_controls_fit_a_fingertip_and_fields_do_not_zoom(self):
        with sync_playwright() as p:
            page = self.open(p, self.feature + "/diff?mode=systems", ".hunk")
            for selector in (".segmented button", ".ctx-btn", ".mark-btn", ".tabs a", ".search-link"):
                self.assertGreaterEqual(page.locator(selector).first.bounding_box()["height"], 36, selector)
            page.locator("tr.add .ln-btn").first.click()
            size = page.locator(".composer textarea").evaluate("el => getComputedStyle(el).fontSize")
            self.assertEqual(size, "16px")  # iOS zooms into anything smaller
            wide = self.open(p, self.feature + "/diff?mode=systems", ".hunk", viewport={"width": 1280, "height": 800},
                             is_mobile=False, has_touch=False)
            self.assertLess(wide.locator(".mark-btn").first.bounding_box()["height"], 30)  # desktop stays dense
            self.assertEqual(page.errors + wide.errors, [])

    def test_review_bar_moves_comments_and_marks_by_touch(self):
        with sync_playwright() as p:
            page = self.open(p, self.feature + "/diff?mode=systems", ".hunk")
            bar = page.locator(".review-bar")
            bar.wait_for()
            # The hunk the bar acts on is outlined; the arrows move it.
            first = page.locator(".hunk.current").get_attribute("data-hunk")
            self.assertEqual(first, page.locator(".hunk").first.get_attribute("data-hunk"))
            bar.locator("button[aria-label='Next hunk']").tap()
            self.assertEqual(page.locator(".hunk.current").get_attribute("data-hunk"), page.locator(".hunk").nth(1).get_attribute("data-hunk"))
            bar.locator("button[aria-label='Previous hunk']").tap()
            self.assertEqual(page.locator(".hunk.current").get_attribute("data-hunk"), first)
            # Tap a line to pick it, then comment on it from the bar.
            row = page.locator("tr.add", has_text="compute_the_answer")
            row.locator("td.src").tap()
            self.assertIn("picked", row.get_attribute("class"))
            bar.locator("button", has_text="Comment on line 3").tap()
            page.locator(".composer textarea").fill("Why four arguments?")
            self.assertFalse(bar.is_visible())  # the keyboard needs the room
            page.locator(".composer button[type=submit]").tap()
            page.locator(".comment-row", has_text="Why four arguments?").wait_for()
            bar.wait_for()
            comments = page.request.get(self.base + f"api/v1/projects/{self.pid}/features/feat--phone").json()["comments"]
            self.assertEqual([(c["anchor"]["path"], c["anchor"]["line"]) for c in comments], [("app.py", 3)])
            # Mark the outlined hunk reviewed.
            bar.locator(".mark-toggle").tap()
            page.locator(".hunk.current .mark-btn[aria-pressed=true]").wait_for()
            self.assertEqual(bar.locator(".mark-toggle").get_attribute("aria-pressed"), "true")
            # Desktop has the keys instead.
            wide = self.open(p, self.feature + "/diff?mode=systems", ".hunk", viewport={"width": 1280, "height": 800},
                             is_mobile=False, has_touch=False)
            self.assertFalse(wide.locator(".review-bar").is_visible())
            self.assertEqual(page.errors + wide.errors, [])

    def test_review_bar_offers_only_what_the_page_allows(self):
        from debrief import export
        from debrief.api import Api

        out = self.tmp / "export.html"
        out.write_text(export.render(Api(self.archive), self.pid, "feat--phone"))
        with sync_playwright() as p:
            page = self.open(p, self.feature + "/diff?mode=story", ".hunk")
            bar = page.locator(".review-bar")
            bar.wait_for()
            self.assertFalse(bar.locator(".mark-toggle").is_visible())  # marks belong to the feature diff
            self.assertTrue(bar.locator("button", has_text="Comment").is_visible())
            page.goto(out.as_uri() + f"#/p/{self.pid}/f/feat--phone/diff?mode=systems")
            page.locator(".hunk").first.wait_for()
            bar.wait_for()
            self.assertFalse(bar.locator("button", has_text="Comment").is_visible())  # a read-only copy
            self.assertFalse(bar.locator(".mark-toggle").is_visible())
            self.assertTrue(bar.locator("button[aria-label='Next hunk']").is_visible())
            page.goto(self.base + self.feature + "/tests")
            page.locator("table.tests-table").wait_for()
            self.assertFalse(bar.is_visible())  # no hunks, no bar
            self.assertEqual(page.errors, [])  # the export's hash-locked CSP allows everything above

    def test_search_and_help_suit_the_screen(self):
        with sync_playwright() as p:
            page = self.open(p, self.feature, ".feature-head")
            self.assertFalse(page.locator(".searchbox").is_visible())
            self.assertFalse(page.locator(".help-btn").is_visible())
            page.locator(".search-link").click()
            page.locator(".search-page input[type=search]").wait_for()
            self.assertTrue(page.url.endswith("#/search"))
            wide = self.open(p, self.feature, ".feature-head", viewport={"width": 1280, "height": 800}, is_mobile=False, has_touch=False)
            self.assertTrue(wide.locator(".searchbox").is_visible())
            self.assertTrue(wide.locator(".help-btn").is_visible())
            self.assertFalse(wide.locator(".search-link").is_visible())
            self.assertEqual(page.errors + wide.errors, [])


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

import unittest

from tests.helpers import IsolatedTestCase

from debrief import records


class FrontmatterTests(unittest.TestCase):
    def test_parses_frontmatter_and_body(self):
        meta, body, error = records.split_frontmatter("---\nid: x\nlist: [1, 2]\n---\n\n## Purpose\nHi\n")
        self.assertIsNone(error)
        self.assertEqual(meta, {"id": "x", "list": [1, 2]})
        self.assertIn("## Purpose", body)

    def test_empty_frontmatter_is_a_mapping(self):
        meta, body, error = records.split_frontmatter("---\n---\nbody\n")
        self.assertEqual(meta, {})
        self.assertEqual(body, "body\n")
        self.assertIsNone(error)

    def test_invalid_yaml_reports_error(self):
        meta, _body, error = records.split_frontmatter("---\nid: [unclosed\n---\n")
        self.assertIsNone(meta)
        self.assertIn("not valid YAML", error)

    def test_no_frontmatter(self):
        meta, body, error = records.split_frontmatter("# Title\n")
        self.assertIsNone(meta)
        self.assertIsNone(error)
        self.assertEqual(body, "# Title\n")

    def test_dates_become_strings(self):
        meta, _, _ = records.split_frontmatter("---\nwhen: 2026-10-02\n---\n")
        self.assertEqual(meta["when"], "2026-10-02")

    def test_json_frontmatter_is_accepted(self):
        meta, _, error = records.split_frontmatter('---\n{"id": "x", "change": "new"}\n---\n')
        self.assertIsNone(error)
        self.assertEqual(meta["change"], "new")


class SectionTests(unittest.TestCase):
    def test_sections_split_on_level_two(self):
        title, sections, order = records.parse_sections(
            "# Title\n\nIntro\n\n## Purpose\nA\n### Detail\nB\n\n## Change\nC\n```\n## not a heading\n```\n")
        self.assertEqual(title, "Title")
        self.assertEqual(order, ["Purpose", "Change"])
        self.assertIn("### Detail", sections["Purpose"])
        self.assertIn("## not a heading", sections["Change"])

    def test_find_section_is_lenient(self):
        _, sections, _ = records.parse_sections("## Risks & Gaps\nx\n")
        self.assertEqual(records.find_section(sections, "Risks and gaps"), "x")


class AnchorTests(unittest.TestCase):
    def test_dict_anchor(self):
        anchor, problem = records.normalize_anchor({"path": "./src/a.py", "symbol": "A.b", "lines": "40-88"})
        self.assertIsNone(problem)
        self.assertEqual(anchor, {"path": "src/a.py", "symbol": "A.b", "lines": [40, 88], "role": None})

    def test_string_forms(self):
        self.assertEqual(records.normalize_anchor("src/a.py:A.b")[0]["symbol"], "A.b")
        self.assertEqual(records.normalize_anchor("src/a.py:10-12")[0]["lines"], [10, 12])
        self.assertEqual(records.normalize_anchor("src/a.py#L3-L5")[0]["lines"], [3, 5])
        anchor, problem = records.normalize_anchor("src/a.py")
        self.assertIsNone(anchor["symbol"])
        self.assertIn("string", problem)

    def test_bad_lines_and_missing_path(self):
        anchor, problem = records.normalize_anchor({"path": "a.py", "lines": "x-y"})
        self.assertIsNone(anchor["lines"])
        self.assertIn("not a line", problem)
        self.assertEqual(records.normalize_anchor({"symbol": "x"}), (None, "anchor has no path"))

    def test_line_forms(self):
        self.assertEqual(records.parse_lines(7), (7, 7))
        self.assertEqual(records.parse_lines([9, 3]), (3, 9))
        self.assertEqual(records.parse_lines("12"), (12, 12))
        self.assertIsNone(records.parse_lines(0))
        self.assertIsNone(records.parse_lines(True))


class GlobTests(unittest.TestCase):
    def test_glob_match(self):
        self.assertTrue(records.glob_match("vendor/**", "vendor/a/b.py"))
        self.assertTrue(records.glob_match("src/*.py", "src/a.py"))
        self.assertFalse(records.glob_match("src/*.py", "src/x/a.py"))
        self.assertTrue(records.glob_match("**/LICENSE", "a/b/LICENSE"))
        self.assertTrue(records.glob_match("**/LICENSE", "LICENSE"))
        self.assertTrue(records.glob_match("docs/", "docs/x.md"))
        self.assertFalse(records.glob_match("README.md", "docs/README.md"))
        self.assertTrue(records.is_incidental("a.lock", ["*.lock"]))


class JournalTests(unittest.TestCase):
    def test_entries_and_issues(self):
        text = ("# Journal\n\n### 2026-10-02T19:41Z · plan\nDo it.\n\n"
                "### 2026-10-02T19:50Z · banter\nhi\n### yesterday · test\nran\n")
        entries, issues = records.parse_journal(text)
        self.assertEqual([e["kind"] for e in entries], ["plan", "banter", "test"])
        self.assertEqual(entries[0]["text"], "Do it.")
        messages = " ".join(i["message"] for i in issues)
        self.assertIn("unknown entry kind `banter`", messages)
        self.assertIn("`yesterday` is not a UTC time", messages)

    def test_ascii_separator(self):
        entries, issues = records.parse_journal("### 2026-10-02T19:41Z - decision\nx\n")
        self.assertEqual(entries[0]["kind"], "decision")
        self.assertEqual(issues, [])


class FeatureTests(IsolatedTestCase):
    def test_load_feature_and_cross_checks(self):
        fdir = self.tmp / "archive" / "projects" / "p" / "features" / "feat--x"
        self.write_records(fdir, systems=["core"], brief_extra="review_first:\n  - {target: core/missing, why: x}\n")
        # review_first appears twice now; YAML keeps the last value.
        self.write(fdir / "systems" / "core.md", (
            "---\nid: core\ntitle: Core\nchange: modified\ndepends_on: [ghost]\n"
            "anchors: [{path: app.py}]\ncritical_paths:\n  - {id: loop, kind: loop, anchor: {path: app.py, symbol: main}}\n"
            "decisions: [001-nope]\n---\n\n## Purpose\nP\n"))
        self.write(fdir / "tests.yaml", "tests:\n  - {id: t1, validates: [core/loop, other], claim: c, kind: unit, command: x, claimed_result: pass}\n")
        feature = records.load_feature(fdir)
        self.assertEqual(feature["feature_id"], "feat--x")
        self.assertEqual(feature["project_id"], "p")
        messages = [i["message"] for i in records.all_issues(feature)]
        joined = "\n".join(messages)
        self.assertIn("unknown system `ghost`", joined)
        self.assertIn("only path anchors", joined)
        self.assertIn("has no invariant", joined)
        self.assertIn("unknown decision `001-nope`", joined)
        self.assertIn("validates unknown `other`", joined)
        self.assertIn("`gaps` is required", joined)
        self.assertIn("review_first target `core/missing`", joined)
        self.assertIn("missing or empty sections: Change, How it works, Limitations", joined)

    def test_closeout_issues(self):
        fdir = self.tmp / "f"
        fdir.mkdir()
        feature = records.load_feature(fdir)
        self.assertEqual(len(records.closeout_issues(feature)), 3)
        self.write_records(fdir)
        feature = records.load_feature(fdir)
        self.assertEqual(records.closeout_issues(feature), [])
        self.assertEqual([i for i in records.all_issues(feature) if i["level"] == "error"], [])

    def test_sloppy_field_types_are_read_as_text_and_indexed(self):
        from debrief import index

        fdir = self.archive / "projects" / "p" / "features" / "feat--sloppy"
        self.write_records(fdir)
        brief = (fdir / "brief.md").read_text().replace("title: Test feature", "title: [Fix, the, parser]") \
            .replace("status: ready_for_review", "status: yes")
        self.write(fdir / "brief.md", brief.replace("incidental: []\n", "incidental: []\nepic: {a: 1}\n"))
        self.write(fdir / "tests.yaml", "tests:\n  - {id: t1, validates: [app], claim: c, kind: unit, command: x, "
                                        "claimed_result: yes}\ngaps: []\n")
        feature = records.load_feature(fdir)
        meta = feature["brief"]["meta"]
        self.assertEqual((meta["title"], meta["status"]), ("Fix the parser", "true"))
        self.assertIsInstance(meta["epic"], str)
        self.assertEqual(feature["tests"]["tests"][0]["claimed_result"], "true")
        joined = "\n".join(i["message"] for i in records.all_issues(feature))
        self.assertIn("`title` should be text", joined)
        self.assertIn("claimed_result 'true' is not one of", joined)
        self.write(self.archive / "projects" / "p" / "project.json", '{"project_id": "p"}')
        self.assertEqual(index.rebuild(self.archive), 1)
        self.assertEqual(index.list_features(self.archive)[0]["title"], "Fix the parser")

    def test_one_unindexable_feature_does_not_break_the_index(self):
        from unittest import mock

        from debrief import index

        for fid in ("feat--good", "feat--bad"):
            self.write_records(self.archive / "projects" / "p" / "features" / fid)
        self.write(self.archive / "projects" / "p" / "project.json", '{"project_id": "p"}')
        real = index._write_feature

        def flaky(conn, pid, fid, feature, evidence):
            if fid == "feat--bad":
                conn.execute("INSERT INTO search VALUES ('p', 'feat--bad', 'zqzq', 'zqzq', 'zqzq', 'zqzq')")
                raise TypeError("unsupported type")
            return real(conn, pid, fid, feature, evidence)

        with mock.patch.object(index, "_write_feature", flaky), mock.patch("sys.stderr"):
            self.assertEqual(index.rebuild(self.archive), 2)
        rows = {r["feature_id"]: r for r in index.list_features(self.archive)}
        self.assertEqual(rows["feat--good"]["title"], "Test feature")
        self.assertIn("could not index", rows["feat--bad"]["summary"])
        self.assertEqual(index.search("zqzq", root=self.archive), [])


if __name__ == "__main__":
    unittest.main()

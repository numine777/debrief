"""Diff parsing, symbol resolution and flag rules."""

import json
import unittest

from tests.helpers import IsolatedTestCase

from debrief import diffparse, rules, symbols

SAMPLE_DIFF = """diff --git a/src/q.py b/src/q.py
index 1111111111111111111111111111111111111111..2222222222222222222222222222222222222222 100644
--- a/src/q.py
+++ b/src/q.py
@@ -10,4 +10,5 @@ class Queue:
     def drain(self):
-        return 1
+        while self.items:
+            self.pop()

     def other(self):
@@ -40,0 +42,2 @@ def tail():
+x = 1
+y = 2
diff --git a/new file.txt b/new file.txt
new file mode 100644
index 0000000000000000000000000000000000000000..3333333333333333333333333333333333333333
--- /dev/null
+++ b/new file.txt
@@ -0,0 +1,2 @@
+hello
+world
\\ No newline at end of file
diff --git a/old.py b/old.py
deleted file mode 100644
index 4444444444444444444444444444444444444444..0000000000000000000000000000000000000000
--- a/old.py
+++ /dev/null
@@ -1,2 +0,0 @@
-a = 1
-b = 2
diff --git a/a.txt b/b.txt
similarity index 100%
rename from a.txt
rename to b.txt
diff --git a/logo.png b/logo.png
index 5555555555555555555555555555555555555555..6666666666666666666666666666666666666666 100644
Binary files a/logo.png and b/logo.png differ
"""


class DiffParseTests(unittest.TestCase):
    def setUp(self):
        self.files = {f.path: f for f in diffparse.parse(SAMPLE_DIFF)}

    def test_statuses_and_blobs(self):
        self.assertEqual(self.files["src/q.py"].status, "M")
        self.assertEqual(self.files["src/q.py"].old_blob, "1" * 40)
        self.assertEqual(self.files["new file.txt"].status, "A")
        self.assertIsNone(self.files["new file.txt"].old_blob)
        self.assertEqual(self.files["old.py"].status, "D")
        self.assertIsNone(self.files["old.py"].new_blob)
        self.assertEqual((self.files["b.txt"].status, self.files["b.txt"].old_path, self.files["b.txt"].similarity),
                         ("R", "a.txt", 100))
        self.assertTrue(self.files["logo.png"].binary)

    def test_hunks_and_numbering(self):
        first, second = self.files["src/q.py"].hunks
        self.assertEqual((first.old_start, first.old_len, first.new_start, first.new_len), (10, 4, 10, 5))
        self.assertEqual(first.section, "class Queue:")
        numbered = list(first.numbered())
        self.assertEqual(numbered[1], ("-", 11, None, "        return 1"))
        self.assertEqual(numbered[2], ("+", None, 11, "        while self.items:"))
        self.assertEqual(second.new_range(), (42, 43))
        self.assertEqual(self.files["old.py"].hunks[0].new_range(), (1, 1))
        new_file = self.files["new file.txt"].hunks[0]
        self.assertEqual(new_file.lines[-1][0], "\\")
        self.assertEqual(new_file.additions, 2)

    def test_hunk_id_ignores_line_numbers(self):
        shifted = SAMPLE_DIFF.replace("@@ -10,4 +10,5 @@", "@@ -90,4 +95,5 @@")
        a = diffparse.parse(SAMPLE_DIFF)[0].hunks[0].id
        b = diffparse.parse(shifted)[0].hunks[0].id
        self.assertEqual(a, b)
        changed = SAMPLE_DIFF.replace("+            self.pop()", "+            self.pop(0)")
        self.assertNotEqual(a, diffparse.parse(changed)[0].hunks[0].id)

    def test_quoted_paths(self):
        text = ('diff --git "a/sp\\303\\251c.txt" "b/sp\\303\\251c.txt"\nnew file mode 100644\n'
                "index 0000000000000000000000000000000000000000..1111111111111111111111111111111111111111\n"
                '--- /dev/null\n+++ "b/sp\\303\\251c.txt"\n@@ -0,0 +1 @@\n+x\n')
        self.assertEqual(diffparse.parse(text)[0].path, "spéc.txt")

    def test_line_map(self):
        mapping = diffparse.line_map("a\nb\nc\nd\n", "z\na\nc\nd\ne\n")
        self.assertEqual(mapping, {1: 2, 3: 3, 4: 4})
        self.assertEqual(diffparse.map_range(mapping, 2, 3), (3, 3))
        self.assertIsNone(diffparse.map_range(mapping, 2, 2))

    def test_noise(self):
        self.assertEqual(diffparse.classify_noise("web/package-lock.json"), "lockfile")
        self.assertEqual(diffparse.classify_noise("static/app.min.js"), "generated")
        self.assertEqual(diffparse.classify_noise("third_party/zlib/zlib.h"), "vendored")
        self.assertIsNone(diffparse.classify_noise("src/app.py"))


class SymbolTests(unittest.TestCase):
    def names(self, path, text):
        return {s.name: (s.start, s.end) for s in symbols.index(path, text)}

    def test_python(self):
        text = ("import x\n\nLIMIT = 3\n\n\nclass Queue:\n    size = 1\n\n    @property\n    def drain(self):\n"
                "        def inner():\n            pass\n        return inner\n\n\nasync def main():\n    pass\n")
        names = self.names("q.py", text)
        self.assertEqual(names["LIMIT"], (3, 3))
        self.assertEqual(names["Queue"], (6, 13))
        self.assertEqual(names["Queue.drain"], (9, 13))
        self.assertEqual(names["Queue.drain.inner"], (11, 12))
        self.assertEqual(names["Queue.size"], (7, 7))
        self.assertEqual(names["main"], (16, 17))

    def test_python_syntax_error_falls_back(self):
        names = self.names("bad.py", "def ok():\n    return 1\n\ndef broken(:\n    pass\n")
        self.assertIn("ok", names)

    def test_go_rust_js_cpp(self):
        go = "type Q struct {\n\tn int\n}\n\nfunc (q *Q) Drain() {\n\tfor {\n\t}\n}\n"
        self.assertEqual(self.names("q.go", go)["Q.Drain"], (5, 8))
        rust = "impl Q {\n    pub fn drain(&mut self) {\n        loop {}\n    }\n}\n"
        self.assertEqual(self.names("q.rs", rust)["Q.drain"], (2, 4))
        js = "export class Q {\n  async drain() {\n    if (a) { b(); }\n  }\n}\nconst f = (x) => {\n  return x;\n};\n"
        names = self.names("q.ts", js)
        self.assertEqual(names["Q.drain"], (2, 4))
        self.assertEqual(names["f"], (6, 8))
        cpp = "namespace n {\nclass C {\n  int Run(int x) {\n    return x;\n  }\n};\n}\n"
        self.assertEqual(self.names("c.cc", cpp)["n.C.Run"], (3, 5))

    def test_braces_in_strings_and_comments(self):
        js = 'function f() {\n  const s = "}";\n  // }\n  /* } */\n  return s;\n}\nfunction g() {}\n'
        names = self.names("a.js", js)
        self.assertEqual(names["f"], (1, 6))
        self.assertEqual(names["g"], (7, 7))

    def test_build_files_markdown_and_config(self):
        bzl = 'cc_library(\n    name = "core",\n    srcs = ["a.cc"],\n)\n'
        self.assertEqual(self.names("pkg/BUILD", bzl)["core"], (1, 4))
        md = "# T\n\n## Purpose\nx\n\n## Change\ny\n"
        names = self.names("doc.md", md)
        self.assertEqual(names["Purpose"], (3, 5))
        yaml_text = "build:\n  a: 1\n\ntest:\n  b: 2\n"
        self.assertEqual(self.names("ci.yaml", yaml_text)["build"], (1, 2))
        toml_text = "[tool.x]\na = 1\n\n[project]\nname = 'p'\n"
        self.assertEqual(self.names("pyproject.toml", toml_text)["tool.x"], (1, 2))
        self.assertIn("build", self.names("Makefile", "build:\n\tcc a.c\n"))

    def test_find(self):
        syms = symbols.index("q.py", "class Queue:\n    def drain(self):\n        pass\n\ndef drain_all():\n    pass\n")
        self.assertEqual(symbols.find(syms, "Queue.drain").name, "Queue.drain")
        self.assertEqual(symbols.find(syms, "drain").name, "Queue.drain")
        self.assertEqual(symbols.find(syms, "Queue::drain()").name, "Queue.drain")
        self.assertEqual(symbols.find(syms, "pkg.Queue").name, "Queue")
        self.assertIsNone(symbols.find(syms, "missing"))
        self.assertEqual(symbols.enclosing(syms, 3).name, "Queue.drain")


class RuleTests(IsolatedTestCase):
    def scan(self, path, lines, removed=()):
        body = "".join(f"+{line}\n" for line in lines) + "".join(f"-{line}\n" for line in removed)
        diff = (f"diff --git a/{path} b/{path}\nindex 1111111..2222222 100644\n--- a/{path}\n+++ b/{path}\n"
                f"@@ -1,{len(removed)} +1,{len(lines)} @@\n{body}")
        hunks = diffparse.parse(diff)[0].hunks
        return {f["rule"] for f in rules.scan(rules.load(), path, hunks)}

    def test_code_rules(self):
        found = self.scan("src/w.py", ["while True:", "    time.sleep(1)", "    requests.get(url)",
                                       "try:", "    x()", "except Exception:", "    pass  # TODO later"])
        self.assertTrue({"loop", "sleep", "external-io", "error-handling", "broad-except", "todo"} <= found, found)

    def test_strings_and_comments_do_not_fire_code_rules(self):
        found = self.scan("src/w.py", ['KINDS = ("retry", "while")', "x = 1  # retry later"])
        self.assertNotIn("retry", found)
        self.assertNotIn("loop", found)

    def test_test_rules_apply_to_test_files_only(self):
        self.assertIn("test-skipped", self.scan("tests/test_q.py", ["@pytest.mark.skip"]))
        self.assertIn("assertion-removed", self.scan("tests/test_q.py", [], removed=["    assert x == 1"]))
        self.assertNotIn("assertion-removed", self.scan("src/q.py", [], removed=["    assert x == 1"]))
        self.assertNotIn("loop", self.scan("tests/test_q.py", ["while True:"]))

    def test_dependency_rule_fires_once(self):
        found = rules.scan(rules.load(), "pyproject.toml", diffparse.parse(
            "diff --git a/pyproject.toml b/pyproject.toml\nindex 1..2 100644\n--- a/pyproject.toml\n+++ b/pyproject.toml\n"
            "@@ -1,0 +1,2 @@\n+requests = '*'\n+httpx = '*'\n")[0].hunks)
        self.assertEqual([f["rule"] for f in found].count("dependency-change"), 1)

    def test_project_override(self):
        project = self.tmp / "proj"
        project.mkdir()
        (project / "rules.json").write_text(json.dumps({
            "disable": ["todo"], "severity": {"loop": "low"},
            "rules": [{"id": "no-print", "category": "risky", "severity": "low", "pattern": r"\bprint\(", "message": "print"}]}))
        loaded = {r.id: r for r in rules.load(project)}
        self.assertNotIn("todo", loaded)
        self.assertEqual(loaded["loop"].severity, "low")
        self.assertIn("no-print", loaded)


if __name__ == "__main__":
    unittest.main()

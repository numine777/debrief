import json
import os
import unittest

from tests.helpers import IsolatedTestCase

from debrief import install


class InstallTests(IsolatedTestCase):
    def test_detects_and_installs_for_claude_and_codex(self):
        (self.home / ".claude").mkdir()
        (self.home / ".codex").mkdir()
        self.write(self.home / ".claude" / "CLAUDE.md", "# My rules\n\nBe nice.\n")
        report = "\n".join(install.run_install())
        self.assertIn("claude: detected", report)
        self.assertIn("codex: detected", report)
        claude_md = (self.home / ".claude" / "CLAUDE.md").read_text()
        self.assertTrue(claude_md.startswith("# My rules\n\nBe nice.\n\n<!-- debrief:begin v1 -->"))
        self.assertIn("~/.agents/skills/ai-session/SKILL.md", claude_md)
        self.assertNotIn("<skills>", claude_md)
        self.assertIn("<!-- debrief:begin v1 -->", (self.home / ".codex" / "AGENTS.md").read_text())
        skill = self.home / ".agents" / "skills" / "ai-session" / "SKILL.md"
        self.assertTrue(skill.exists())
        self.assertTrue((self.home / ".agents" / "skills" / "ai-session" / "templates" / "system.md").exists())
        link = self.home / ".claude" / "skills" / "ai-session-closeout"
        self.assertTrue(link.is_symlink())
        self.assertTrue((link / "SKILL.md").exists())
        launcher = self.tmp / "bin" / "debrief-session"
        self.assertTrue(os.access(launcher, os.X_OK))
        self.assertIn("session ", launcher.read_text())
        self.assertTrue((self.tmp / "tool" / "debrief.pyz").exists())

    def test_reinstall_is_idempotent_and_upgrade_replaces_only_the_block(self):
        (self.home / ".codex").mkdir()
        install.run_install()
        target = self.home / ".codex" / "AGENTS.md"
        first = target.read_text()
        report = "\n".join(install.run_install())
        self.assertIn("codex: unchanged", report)
        self.assertEqual(target.read_text(), first)
        old = first.replace("<!-- debrief:begin v1 -->", "<!-- debrief:begin v0 -->").replace("Rules", "Old rules")
        target.write_text("Before\n\n" + old + "\nAfter\n")
        report = "\n".join(install.run_install())
        self.assertIn("codex: updated", report)
        text = target.read_text()
        self.assertTrue(text.startswith("Before\n\n<!-- debrief:begin v1 -->"))
        self.assertTrue(text.endswith("\nAfter\n"))
        self.assertNotIn("Old rules", text)
        self.assertEqual(text.count("debrief:begin"), 1)

    def test_devin_and_claude_share_one_block(self):
        (self.home / ".claude").mkdir()
        (self.home / ".config" / "devin").mkdir(parents=True)
        self.write(self.home / ".config" / "devin" / "AGENTS.md", "keep\n\n" + install.render_block())
        report = "\n".join(install.run_install())
        self.assertIn("removed the duplicate block", report)
        self.assertEqual((self.home / ".config" / "devin" / "AGENTS.md").read_text(), "keep\n")
        self.assertIn("debrief:begin", (self.home / ".claude" / "CLAUDE.md").read_text())

    def test_explicit_harness_and_custom_dirs(self):
        os.environ["PI_CODING_AGENT_DIR"] = str(self.tmp / "pi")
        report = "\n".join(install.run_install(["pi"]))
        self.assertIn("pi: added block", report)
        self.assertTrue((self.tmp / "pi" / "AGENTS.md").exists())

    def test_skill_dir_not_ours_is_left_alone(self):
        foreign = self.home / ".agents" / "skills" / "ai-session"
        self.write(foreign / "SKILL.md", "someone else's")
        report = "\n".join(install.run_install(["codex"]))
        self.assertIn("was not installed by Debrief", report)
        self.assertEqual((foreign / "SKILL.md").read_text(), "someone else's")

    def test_claude_settings_merge_and_remove(self):
        (self.home / ".claude").mkdir()
        settings = self.home / ".claude" / "settings.json"
        settings.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo mine"}]}]},
                                        "permissions": {"allow": ["Bash(ls:*)"]}, "theme": "dark"}))
        install.run_install(["claude"], claude_hooks=True)
        install.run_install(["claude"], claude_hooks=True)
        data = json.loads(settings.read_text())
        commands = [h["command"] for entry in data["hooks"]["SessionStart"] for h in entry["hooks"]]
        self.assertEqual(len(commands), 2)
        self.assertIn("echo mine", commands)
        self.assertTrue(any(c.endswith("debrief-session context") for c in commands))
        self.assertIn(str(self.archive), data["permissions"]["additionalDirectories"])
        self.assertIn("Bash(ls:*)", data["permissions"]["allow"])
        self.assertEqual(data["theme"], "dark")
        install.run_uninstall()
        data = json.loads(settings.read_text())
        self.assertEqual(data["hooks"]["SessionStart"][0]["hooks"][0]["command"], "echo mine")
        self.assertEqual(data["permissions"], {"allow": ["Bash(ls:*)"]})

    def test_uninstall_removes_what_install_wrote(self):
        (self.home / ".claude").mkdir()
        self.write(self.home / ".claude" / "CLAUDE.md", "mine\n")
        install.run_install()
        install.run_uninstall()
        self.assertEqual((self.home / ".claude" / "CLAUDE.md").read_text(), "mine\n")
        self.assertFalse((self.home / ".agents" / "skills" / "ai-session").exists())
        self.assertFalse((self.home / ".claude" / "skills" / "ai-session").exists())
        self.assertFalse((self.tmp / "bin" / "debrief").exists())


if __name__ == "__main__":
    unittest.main()

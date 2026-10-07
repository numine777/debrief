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
        self.assertTrue(claude_md.startswith(f"# My rules\n\nBe nice.\n\n<!-- debrief:begin v{install.BUNDLE_VERSION} -->"))
        self.assertIn("~/.agents/skills/ai-session/SKILL.md", claude_md)
        self.assertNotIn("<skills>", claude_md)
        self.assertIn(f"<!-- debrief:begin v{install.BUNDLE_VERSION} -->", (self.home / ".codex" / "AGENTS.md").read_text())
        skill = self.home / ".agents" / "skills" / "ai-session" / "SKILL.md"
        self.assertTrue(skill.exists())
        self.assertTrue((self.home / ".agents" / "skills" / "ai-session" / "templates" / "system.md").exists())
        link = self.home / ".claude" / "skills" / "ai-session-closeout"
        self.assertTrue(link.is_symlink())
        self.assertTrue((link / "SKILL.md").exists())
        launcher = self.tmp / "bin" / "debrief-session"
        self.assertTrue(os.access(launcher, os.X_OK))
        # The instructions name the launcher where install put it, not a fixed ~/.local/bin.
        self.assertIn(f"`bin/session` below means `{launcher}`", claude_md)
        self.assertIn(f"`bin/session` means `{launcher}`", skill.read_text())
        self.assertNotIn("<session>", skill.read_text())
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
        old = first.replace(f"<!-- debrief:begin v{install.BUNDLE_VERSION} -->", "<!-- debrief:begin v0 -->").replace("Rules", "Old rules")
        target.write_text("Before\n\n" + old + "\nAfter\n")
        report = "\n".join(install.run_install())
        self.assertIn("codex: updated", report)
        text = target.read_text()
        self.assertTrue(text.startswith(f"Before\n\n<!-- debrief:begin v{install.BUNDLE_VERSION} -->"))
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
        allow = data["permissions"]["allow"]
        for form in (str(self.tmp / "bin" / "debrief-session"), "debrief-session"):
            self.assertIn(f"Bash({form} now:*)", allow)
        self.assertFalse(any(" run" in rule for rule in allow), "never allowlist run: it executes its argument")
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


class PackagedInstallTests(IsolatedTestCase):
    """Installs where a package (the Nix home-manager module) provides the launchers and skills."""

    def test_render_writes_the_block_and_skills_and_nothing_else(self):
        os.environ.pop("DEBRIEF_BIN_DIR")  # a package renders for the default ~/.local/bin
        out = self.tmp / "agent"
        install.render_agent_files(out)
        self.assertEqual((out / "block.md").read_text(), install.render_block())
        block = (out / "block.md").read_text()
        self.assertIn("`bin/session` below means `~/.local/bin/debrief-session`", block)
        self.assertIn("`~/.agents/skills/ai-session/reference.md`", block)
        skill = (out / "skills" / "ai-session" / "SKILL.md").read_text()
        self.assertIn("`bin/session` means `~/.local/bin/debrief-session`", skill)
        self.assertNotIn("<session>", skill)
        self.assertTrue((out / "skills" / "ai-session-closeout" / "SKILL.md").exists())
        self.assertTrue((out / "skills" / "ai-session" / "templates" / "system.md").exists())
        self.assertFalse((out / "skills" / "ai-session" / install.MANIFEST).exists())
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), [])

    def test_managed_install_keeps_blocks_in_step_with_the_chosen_harnesses(self):
        self.write(self.home / ".claude" / "CLAUDE.md", "mine\n")
        self.write(self.home / ".config" / "devin" / "AGENTS.md", "devin rules\n")
        report = "\n".join(install.run_managed(["claude", "codex", "devin"], claude_hooks=True))
        self.assertIn("claude: added block", report)
        self.assertIn("codex: added block", report)
        self.assertIn("devin: reads the block from ~/.claude/CLAUDE.md", report)
        self.assertEqual((self.home / ".config" / "devin" / "AGENTS.md").read_text(), "devin rules\n")
        settings = json.loads((self.home / ".claude" / "settings.json").read_text())
        self.assertIn("SessionStart", settings["hooks"])
        # Launchers, the zipapp and skills are the package's: none are written.
        self.assertFalse((self.tmp / "bin").exists())
        self.assertFalse((self.tmp / "tool").exists())
        self.assertFalse((self.home / ".agents").exists())
        self.assertIn("unchanged", "\n".join(install.run_managed(["claude", "codex", "devin"], claude_hooks=True)))
        # Dropping a harness takes its block (and Claude's settings) back out.
        report = "\n".join(install.run_managed(["codex"]))
        self.assertIn("claude: removed block", report)
        self.assertEqual((self.home / ".claude" / "CLAUDE.md").read_text(), "mine\n")
        self.assertNotIn("debrief-session", (self.home / ".claude" / "settings.json").read_text())
        install.run_managed(["none"])
        self.assertNotIn("debrief:begin", (self.home / ".codex" / "AGENTS.md").read_text())

    def test_claude_config_dir_moves_claudes_files(self):
        alt = self.tmp / "claude-alt"
        os.environ["CLAUDE_CONFIG_DIR"] = str(alt)
        self.assertIn("claude", install.detect())
        report = "\n".join(install.run_install(["claude", "devin"], claude_hooks=True))
        self.assertIn(f"claude: added block in {alt / 'CLAUDE.md'}", report)
        self.assertTrue((alt / "skills" / "ai-session").is_symlink())
        self.assertIn("debrief-session context", (alt / "settings.json").read_text())
        self.assertFalse((self.home / ".claude").exists())
        # Devin reads ~/.claude/CLAUDE.md, not the moved file, so it keeps its own block.
        self.assertIn("devin: added block", report)
        self.assertIn("debrief:begin", (self.home / ".config" / "devin" / "AGENTS.md").read_text())

    def test_files_nix_manages_are_never_written(self):
        store = self.tmp / "store"
        os.environ["NIX_STORE_DIR"] = str(store)
        self.addCleanup(os.environ.pop, "NIX_STORE_DIR", None)
        links = {
            self.home / ".claude" / "CLAUDE.md": store / "claude.md",
            self.home / ".claude" / "settings.json": store / "settings.json",
            self.tmp / "bin" / "debrief-session": store / "bin" / "debrief-session",
            self.home / ".agents" / "skills" / "ai-session": store / "skills" / "ai-session",
            self.home / ".claude" / "skills" / "ai-session": store / "skills" / "ai-session",
        }
        self.write(store / "claude.md", "declared rules\n")
        self.write(store / "settings.json", "{}\n")
        self.write(store / "bin" / "debrief-session", "#!/bin/sh\n")
        self.write(store / "skills" / "ai-session" / "SKILL.md", "packaged\n")
        for link, target in links.items():
            link.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(target, link)
        report = "\n".join(install.run_managed(["claude"], claude_hooks=True))
        self.assertIn("include programs.debrief.blockText there", report)
        report += "\n".join(install.run_install(["claude"], claude_hooks=True))
        self.assertIn("Nix manages it", report)
        self.assertFalse((self.tmp / "tool" / "debrief.pyz").exists())
        install.run_uninstall()
        for link, target in links.items():
            self.assertTrue(link.is_symlink(), link)
            self.assertEqual(os.readlink(link), str(target))
        self.assertEqual((store / "claude.md").read_text(), "declared rules\n")
        self.assertEqual((store / "settings.json").read_text(), "{}\n")


if __name__ == "__main__":
    unittest.main()

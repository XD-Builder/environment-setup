"""Tests for skill discovery, validation, and markdown load/save."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lmloop import skills


class SkillsDiscoveryTests(unittest.TestCase):
    def test_list_skills_excludes_system(self):
        names = skills.list_skills()
        self.assertIn("ceo", names)
        self.assertIn("investigate", names)
        self.assertNotIn("system", names)
        self.assertNotIn("_author", names)
        self.assertNotIn("_graph_mine", names)
        self.assertNotIn("_reconcile", names)

    def test_skill_blurb(self):
        blurb = skills.skill_blurb("ceo")
        self.assertTrue(blurb)
        self.assertNotIn("#", blurb.split()[0] if blurb else "")

    def test_load_skill_missing(self):
        with self.assertRaises(FileNotFoundError) as ctx:
            skills.load_skill("no-such-skill-xyz")
        self.assertIn("Available:", str(ctx.exception))

    def test_load_author_skill(self):
        text = skills.load_skill("_author")
        self.assertTrue(text.lower().startswith("# skill author"))

    def test_validate_skill_name(self):
        self.assertIsNone(skills.validate_skill_name("deploy"))
        self.assertIsNone(skills.validate_skill_name("compact"))  # skill override ok
        self.assertIsNone(skills.validate_skill_name("retro"))
        self.assertIsNotNone(skills.validate_skill_name("until"))
        self.assertIsNotNone(skills.validate_skill_name("New"))
        self.assertIsNotNone(skills.validate_skill_name("new"))
        self.assertIsNotNone(skills.validate_skill_name("_hidden"))
        self.assertIsNotNone(skills.validate_skill_name("transcript"))
        self.assertIsNotNone(skills.validate_skill_name("copy"))
        self.assertIsNotNone(skills.validate_skill_name("q"))

    def test_load_skill_public_only_hides_private(self):
        with self.assertRaises(FileNotFoundError):
            skills.load_skill("_author", public_only=True)
        with self.assertRaises(FileNotFoundError):
            skills.load_skill("system", public_only=True)
        # internal load still works
        self.assertTrue(skills.load_skill("_author"))

    def test_load_skill_rejects_path_like_name(self):
        with self.assertRaises(FileNotFoundError):
            skills.load_skill("../etc/passwd")

    def test_user_skill_overrides_packaged(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "ceo.md").write_text("# Skill: ceo — user override\n\nUser body\n")
            with patch.object(skills, "USER_SKILLS_DIR", root):
                text = skills.load_skill("ceo")
                self.assertIn("user override", text)
                path = skills.skill_path("ceo")
                self.assertEqual(path, root / "ceo.md")

    def test_extract_skill_markdown_strips_fence(self):
        raw = "```markdown\n# Skill: demo — x\n\nHello\n```"
        out = skills.extract_skill_markdown(raw)
        self.assertTrue(out.startswith("# Skill: demo"))
        self.assertNotIn("```", out)

    def test_save_user_skill(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with patch.object(skills, "USER_SKILLS_DIR", root):
                path = skills.save_user_skill("demo", "# Skill: demo — test\n\nBody\n")
                self.assertEqual(path, root / "demo.md")
                self.assertTrue(path.exists())
                self.assertIn("demo", skills.list_skills())


class SystemPromptMemoryTests(unittest.TestCase):
    def test_system_prompt_requires_memory_disclosure(self):
        text = skills.load_skill("system")
        self.assertIn("Prior learning applied: <key>", text)
        self.assertIn("Decision referenced: [id]", text)


if __name__ == "__main__":
    unittest.main()

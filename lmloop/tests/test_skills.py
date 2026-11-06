"""Tests for skill discovery, validation, and markdown load/save."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lmloop.prompts import skills


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


class GrillAndDisciplineTests(unittest.TestCase):
    def test_grill_is_public_and_slash_loadable(self):
        self.assertIn("grill", skills.list_skills())
        body = skills.load_skill("grill", public_only=True)
        self.assertIn("Index: no", body)
        self.assertIn("GLOSSARY.md", body)
        self.assertIn("docs/adr/", body)
        self.assertIn("Do not ask whether to record it", body)

    def test_grill_stays_out_of_the_index_and_read_skill_refuses(self):
        index = skills.skill_index_block()
        self.assertIn("`tdd`:", index)
        self.assertNotIn("`grill`", index)
        refused = skills.read_skill("grill")
        self.assertIn("The user starts this with /grill", refused)
        self.assertNotIn("GLOSSARY.md", refused)
        self.assertEqual(skills.read_skill("grill", "loop"), refused)

    def test_user_index_opt_out_is_refused_by_read_skill(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "interview.md").write_text(
                "# Skill: interview — ask\n\nUse when: the user asked.\n\n"
                "Index: no\n\nSECRET_INTERVIEW\n",
                encoding="utf-8",
            )
            with patch.object(skills, "USER_SKILLS_DIR", root):
                self.assertIn("interview", skills.list_skills())
                self.assertNotIn("`interview`", skills.skill_index_block())
                self.assertIn("/interview", skills.read_skill("interview"))
                self.assertIn("SECRET_INTERVIEW", skills.load_skill("interview"))

    def test_investigate_requires_a_red_command_before_hypotheses(self):
        text = skills.load_skill("investigate")
        self.assertIn("No hypothesis until a command", text)
        loop = skills.read_skill("investigate", "loop")
        self.assertIn("minimised command", loop)
        self.assertIn("Do not show the list for approval", loop)

    def test_tdd_rejects_a_tautological_assertion(self):
        text = skills.load_skill("tdd")
        self.assertIn("recomputed the way the code does", text)
        self.assertIn("public behavior", text)

    def test_retro_pitfall_names_the_red_command(self):
        text = skills.load_skill("retro")
        self.assertIn("command that goes red", text)

    def test_steering_reads_an_existing_glossary(self):
        text = (skills.SKILLS_DIR.parent / "steer" / "skills.md").read_text()
        self.assertIn("GLOSSARY.md", text)


class SkillPromptTests(unittest.TestCase):
    def test_skill_prompt_appends_task_once(self):
        body = skills.skill_prompt("learn", "", public_only=True)
        with_task = skills.skill_prompt("learn", " tidy ", public_only=True)
        self.assertTrue(body.startswith("#"))
        self.assertEqual(with_task, body + "\n\nTask: tidy")
        self.assertEqual(with_task.count("Task:"), 1)


class SystemPromptMemoryTests(unittest.TestCase):
    def test_system_prompt_requires_memory_disclosure(self):
        text = skills.load_skill("system")
        self.assertIn("Prior learning applied: <key>", text)
        self.assertIn("Decision referenced: [id]", text)

    def test_standing_harness_is_curiosity_learning_growth(self):
        text = skills.load_skill("system")
        self.assertIn("curiosity, learning, and growth", text)
        self.assertIn("Repo-specific practice is learned", text)
        self.assertIn("## Safety floor", text)
        self.assertIn("several files", text)
        self.assertIn("one response", text)
        seed = (skills.SKILLS_DIR.parents[1] / "spirit" / "seed.md").read_text()
        self.assertIn("Be curious, learn, and grow", seed)
        improve = skills.load_skill("improve")
        self.assertIn("delete one", improve)


if __name__ == "__main__":
    unittest.main()

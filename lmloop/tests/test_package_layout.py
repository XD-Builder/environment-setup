"""The package root stays five Python files; a category stays at most five modules."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "lmloop"
ROOT_FILES = ["__init__.py", "__main__.py", "agent.py", "graph.py", "loop.py"]

# ``__init__.py`` in these directories is a package marker, not a sixth module.
CATEGORIES = (
    "entry",
    "model",
    "capabilities",
    "run",
    "flow",
    "verify",
    "host",
    "core",
    "harness",
    "ui",
)


class PackageLayoutTests(unittest.TestCase):
    def test_package_root_has_five_python_files(self):
        names = sorted(p.name for p in ROOT.glob("*.py"))
        self.assertEqual(names, ROOT_FILES)

    def test_category_modules_stay_at_most_five(self):
        for name in CATEGORIES:
            modules = [
                p.name
                for p in (ROOT / name).glob("*.py")
                if p.name != "__init__.py"
            ]
            self.assertLessEqual(len(modules), 5, msg=f"{name}: {modules}")
        memory = [p.name for p in (ROOT / "memory").glob("*.py")]
        self.assertLessEqual(len(memory), 5, msg=memory)
        self.assertIn("__init__.py", memory)

    def test_prompts_colocate_markdown(self):
        skills = ROOT / "prompts" / "skills"
        steer = ROOT / "prompts" / "steer"
        self.assertTrue((skills / "__init__.py").is_file())
        self.assertTrue((skills / "system.md").is_file())
        self.assertTrue((steer / "__init__.py").is_file())
        self.assertTrue((steer / "time.md").is_file())
        self.assertTrue((ROOT / "flow" / "graphs" / "company.md").is_file())


if __name__ == "__main__":
    unittest.main()

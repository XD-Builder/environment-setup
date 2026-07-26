"""Config load/set quality and memory resolve uniqueness."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lmloop import config as config_mod
from lmloop import memory


class ConfigQualityTests(unittest.TestCase):
    def test_corrupt_json_warns_and_uses_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            path.write_text("{not json")
            with mock.patch.object(config_mod, "CONFIG_PATH", path), \
                 mock.patch.object(config_mod, "_CONFIG_WARNED", False), \
                 mock.patch("sys.stderr"):
                cfg = config_mod.load_config()
            self.assertEqual(cfg["max_rounds"], config_mod.DEFAULTS["max_rounds"])
            self.assertIn("confirm_shell_syntax", cfg)


class MemoryResolveTests(unittest.TestCase):
    def test_ambiguous_stem_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cp = root / "checkpoints"
            cp.mkdir()
            (cp / "20260101-aaaa-foo.md").write_text("a")
            (cp / "20260102-bbbb-foo.md").write_text("b")
            with mock.patch.object(memory, "project_dir", return_value=root):
                self.assertIsNone(memory.resolve_checkpoint("foo"))
                self.assertEqual(
                    memory.resolve_checkpoint("20260101-aaaa-foo").name,
                    "20260101-aaaa-foo.md",
                )


if __name__ == "__main__":
    unittest.main()

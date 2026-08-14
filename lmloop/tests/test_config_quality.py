"""Config load/set quality and memory resolve uniqueness."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lmloop import config as config_mod
from lmloop import memory
from lmloop.config import coerce_config_value, normalize_config


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

    def test_string_int_and_bool_are_coerced(self):
        self.assertEqual(coerce_config_value("max_rounds", "60"), 60)
        self.assertIs(coerce_config_value("stream", "false"), False)
        self.assertIs(coerce_config_value("stream", True), True)
        self.assertEqual(coerce_config_value("temperature", "0.2"), 0.2)

    def test_garbage_int_falls_back_on_load(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            path.write_text(json.dumps({"max_rounds": "nope", "stream": True}))
            with mock.patch.object(config_mod, "CONFIG_PATH", path), \
                 mock.patch.object(config_mod, "_CONFIG_WARNED", False), \
                 mock.patch("sys.stderr"):
                cfg = config_mod.load_config()
            self.assertEqual(cfg["max_rounds"], config_mod.DEFAULTS["max_rounds"])
            self.assertIs(cfg["stream"], True)

    def test_normalize_drops_unknown_and_invalid(self):
        out = normalize_config({
            "max_rounds": "12",
            "stream": "yes",
            "unknown": 1,
            "timeout_s": "abc",
        })
        self.assertEqual(out["max_rounds"], 12)
        self.assertIs(out["stream"], True)
        self.assertNotIn("unknown", out)
        self.assertNotIn("timeout_s", out)


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

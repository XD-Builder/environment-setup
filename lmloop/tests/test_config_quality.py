"""Config load/set quality and memory resolve uniqueness."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lmloop import config as config_mod
from lmloop import memory
from lmloop.config import DEFAULTS, cfg_get, cfg_int, coerce_config_value, normalize_config

# Keys documented in README.md config table (keep in sync with the table).
README_CONFIG_KEYS = frozenset({
    "base_url", "api_key", "model", "max_rounds", "eval_max_rounds", "max_continue_nudges",
    "temperature", "timeout_s", "stream", "context_learnings", "context_decisions",
    "confirm_shell", "confirm_destructive", "confirm_shell_syntax", "autonomous_gates",
    "autonomous_snapshot",
    "shell_timeout_s", "web_timeout_s", "max_tool_output", "auto_start_server", "color",
    "context_length", "context_reserve", "until_max_steps", "until_mine",
    "check_inference", "until_baseline",
    "graph_max_steps", "graph_mine", "use_graph", "vision",
})


class ConfigQualityTests(unittest.TestCase):
    def test_readme_config_keys_round_trip_through_coerce(self):
        for key in README_CONFIG_KEYS:
            self.assertIn(key, DEFAULTS, f"{key} missing from DEFAULTS")
            default = DEFAULTS[key]
            if isinstance(default, bool):
                sample = "false" if default else "true"
            elif isinstance(default, int):
                sample = str(default + 1)
            elif isinstance(default, float):
                sample = str(default)
            else:
                sample = default or "http://127.0.0.1:9999/v1"
            coerced = coerce_config_value(key, sample)
            self.assertIsNotNone(coerced, f"coerce_config_value({key!r}, ...) returned None")
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

    def test_vision_coerces(self):
        self.assertEqual(coerce_config_value("vision", "auto"), "auto")
        self.assertEqual(coerce_config_value("vision", True), "true")
        self.assertEqual(coerce_config_value("vision", "false"), "false")
        self.assertIsNone(coerce_config_value("vision", "maybe"))

    def test_check_mode_coerces(self):
        self.assertEqual(coerce_config_value("check_inference", "OFF"), "off")
        self.assertEqual(coerce_config_value("until_baseline", "auto"), "auto")
        self.assertIsNone(coerce_config_value("until_baseline", "sometimes"))
        self.assertEqual(coerce_config_value("autonomous_snapshot", "GIT"), "git")
        self.assertIsNone(coerce_config_value("autonomous_snapshot", "maybe"))

    def test_cfg_get_keeps_zero_values(self):
        cfg = {"context_length": 0, "context_reserve": 512}
        self.assertEqual(cfg_int(cfg, "context_length"), 0)
        self.assertEqual(cfg_int(cfg, "context_reserve"), 512)
        self.assertEqual(cfg_get(cfg, "max_rounds"), DEFAULTS["max_rounds"])

    def test_tool_limits_match_defaults(self):
        from lmloop import tools
        from lmloop.web import DEFAULT_WEB_TIMEOUT_S

        self.assertEqual(tools.MAX_OUTPUT, DEFAULTS["max_tool_output"])
        self.assertEqual(tools.DEFAULT_SHELL_TIMEOUT_S, DEFAULTS["shell_timeout_s"])
        self.assertEqual(DEFAULT_WEB_TIMEOUT_S, DEFAULTS["web_timeout_s"])


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

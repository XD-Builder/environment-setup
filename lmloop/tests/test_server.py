"""Tests for LMS server bring-up and model metadata."""

import unittest
from unittest.mock import patch

from lmloop.server import LmsClient, ServerError, ensure_server, model_has_vision


class EnsureServerTests(unittest.TestCase):
    def test_uses_configured_model_when_loaded(self):
        cfg = {"base_url": "http://127.0.0.1:1234/v1", "model": "mine",
               "auto_start_server": False}
        with patch("lmloop.server.list_models", return_value=["other", "mine"]):
            self.assertEqual(ensure_server(cfg, echo=lambda *_a: None), "mine")

    def test_falls_back_when_configured_model_missing(self):
        cfg = {"base_url": "http://127.0.0.1:1234/v1", "model": "missing",
               "auto_start_server": False}
        notes = []
        with patch("lmloop.server.list_models", return_value=["loaded"]):
            self.assertEqual(ensure_server(cfg, echo=notes.append), "loaded")
        self.assertTrue(any("missing" in n and "loaded" in n for n in notes))

    def test_starts_server_when_empty(self):
        cfg = {"base_url": "http://127.0.0.1:1234/v1", "model": "m",
               "auto_start_server": True}
        with patch("lmloop.server.list_models", side_effect=[[], ["m"]]), \
             patch("lmloop.server.shutil.which", return_value="/usr/bin/lms"), \
             patch("lmloop.server._run_lms", return_value=True) as run, \
             patch("lmloop.server.time.sleep"):
            self.assertEqual(ensure_server(cfg, echo=lambda *_a: None), "m")
        run.assert_called()

    def test_raises_when_no_models(self):
        cfg = {"base_url": "http://127.0.0.1:1234/v1", "model": "",
               "auto_start_server": False}
        with patch("lmloop.server.list_models", return_value=[]), \
             patch("lmloop.server.shutil.which", return_value=None):
            with self.assertRaises(ServerError) as ctx:
                ensure_server(cfg, echo=lambda *_a: None)
            self.assertIn("No models available", str(ctx.exception))


class VisionCapabilityTests(unittest.TestCase):
    def tearDown(self):
        LmsClient._vision_cache.clear()

    def test_config_true_false(self):
        self.assertTrue(model_has_vision("m", {"vision": "true"}))
        self.assertFalse(model_has_vision("m", {"vision": "false"}))

    def test_auto_uses_native_type_not_name(self):
        entries = [{"id": "qwen3.6-35b-a3b", "type": "vlm"}]
        cfg = {"vision": "auto", "base_url": "http://127.0.0.1:1234/v1"}
        with patch("lmloop.server._native_model_entries", return_value=entries):
            LmsClient._vision_cache.clear()
            self.assertTrue(model_has_vision("qwen3.6-35b-a3b", cfg))
            LmsClient._vision_cache.clear()
            self.assertFalse(model_has_vision("text-only", cfg))


if __name__ == "__main__":
    unittest.main()

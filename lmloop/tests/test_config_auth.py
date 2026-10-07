"""API key resolution and OpenRouter request headers."""

import os
import unittest
from unittest import mock

from lmloop import config as config_mod
from lmloop.chat import _chat_request


class ConfigAuthTests(unittest.TestCase):
    def test_resolve_api_key_prefers_config(self):
        cfg = {"api_key": "from-config"}
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "from-env"}, clear=False):
            self.assertEqual(config_mod.resolve_api_key(cfg), "from-config")

    def test_resolve_api_key_falls_back_to_openrouter_env(self):
        cfg = {"api_key": ""}
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-test"}, clear=False):
            self.assertEqual(config_mod.resolve_api_key(cfg), "sk-or-test")

    def test_resolve_api_key_falls_back_to_lmloop_env(self):
        cfg = {"api_key": ""}
        env = {k: v for k, v in os.environ.items() if k != "OPENROUTER_API_KEY"}
        env["LMLOOP_API_KEY"] = "lmloop-key"
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(config_mod.resolve_api_key(cfg), "lmloop-key")

    def test_chat_request_includes_bearer_when_key_set(self):
        cfg = {
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "secret",
            "temperature": 0.7,
            "stream": True,
        }
        req = _chat_request(cfg, "m", [{"role": "user", "content": "hi"}], None, stream=True)
        self.assertEqual(req.headers["Authorization"], "Bearer secret")
        self.assertEqual(req.headers["Content-type"], "application/json")

    def test_openrouter_extra_headers_from_env(self):
        with mock.patch.dict(
            os.environ,
            {"OPENROUTER_HTTP_REFERER": "https://example.com", "OPENROUTER_X_TITLE": "demo"},
            clear=False,
        ):
            headers = config_mod.openrouter_extra_headers()
        self.assertEqual(headers["HTTP-Referer"], "https://example.com")
        self.assertEqual(headers["X-Title"], "demo")


if __name__ == "__main__":
    unittest.main()

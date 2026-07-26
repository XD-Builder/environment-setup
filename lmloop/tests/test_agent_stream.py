"""Tests for streaming chat assembly, display rules, and act() integration."""

import io
import json
import unittest
from unittest import mock

import urllib.error

from lmloop import agent


def _sse(*chunks) -> bytes:
    parts = []
    for c in chunks:
        if c is None:
            parts.append(b"data: [DONE]\n\n")
        else:
            parts.append(f"data: {json.dumps(c)}\n\n".encode())
    return b"".join(parts)


class FakeResp:
    def __init__(self, body: bytes):
        self._buf = io.BytesIO(body)

    def readline(self):
        return self._buf.readline()

    def read(self):
        return self._buf.read()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class StreamPrinterTests(unittest.TestCase):
    def _collect(self):
        out = []
        return out, agent._StreamPrinter(out.append)

    def test_skips_whitespace_only_rounds(self):
        out, p = self._collect()
        p.feed("\n\n")
        p.feed("  \n")
        self.assertFalse(p.finish())
        self.assertEqual(out, [])

    def test_strips_leading_blank_lines(self):
        out, p = self._collect()
        p.feed("\n\nHello")
        self.assertTrue(p.finish())
        self.assertEqual("".join(out), "Hello\n")

    def test_collapses_trailing_newlines_to_one(self):
        out, p = self._collect()
        p.feed("Hello\n\n\n")
        self.assertTrue(p.finish())
        self.assertEqual("".join(out), "Hello\n")

    def test_single_separator_between_paragraphs(self):
        out, p = self._collect()
        p.feed("A\n\n")
        p.feed("B")
        self.assertTrue(p.finish())
        self.assertEqual("".join(out), "A\nB\n")

    def test_silent_printer_does_not_write_live_plain(self):
        p = agent._StreamPrinter(None)
        p.feed("**hi**")
        self.assertTrue(p.finish())
        self.assertTrue(p.visible)

    def test_resolve_echo_delta_defaults_silent(self):
        self.assertIsNone(agent._resolve_echo_delta(None))
        self.assertIsNone(agent._resolve_echo_delta(False))
        self.assertIs(agent._resolve_echo_delta(True), agent._default_echo_delta)
        custom = lambda p: None
        self.assertIs(agent._resolve_echo_delta(custom), custom)

    def test_emit_silent_calls_echo_once(self):
        echoed = []
        p = agent._StreamPrinter(None)
        p.feed("**hi**")
        agent._emit_assistant_content(echoed.append, "**hi**", p, live_plain=False)
        self.assertEqual(echoed, ["**hi**"])

    def test_emit_live_plain_skips_echo(self):
        echoed = []
        out = []
        p = agent._StreamPrinter(out.append)
        p.feed("hi")
        agent._emit_assistant_content(echoed.append, "hi", p, live_plain=True)
        self.assertEqual(echoed, [])
        self.assertEqual("".join(out), "hi\n")


class MergeToolCallDeltaTests(unittest.TestCase):
    def test_accumulates_arguments_by_index(self):
        acc = {}
        agent._merge_tool_call_delta(acc, [{
            "index": 0,
            "id": "call_1",
            "type": "function",
            "function": {"name": "run_shell", "arguments": ""},
        }])
        agent._merge_tool_call_delta(acc, [{
            "index": 0,
            "function": {"arguments": '{"command":'},
        }])
        agent._merge_tool_call_delta(acc, [{
            "index": 0,
            "function": {"arguments": '"mkdir -p docs"}'},
        }])
        self.assertEqual(acc[0]["function"]["name"], "run_shell")
        self.assertEqual(acc[0]["function"]["arguments"], '{"command":"mkdir -p docs"}')
        self.assertEqual(acc[0]["id"], "call_1")


class ChatStreamTests(unittest.TestCase):
    def test_streams_content_and_usage(self):
        body = _sse(
            {"choices": [{"delta": {"role": "assistant", "content": "Hello"}}]},
            {"choices": [{"delta": {"content": " world"}}]},
            {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}},
            None,
        )
        deltas = []
        cfg = {"base_url": "http://127.0.0.1:1234/v1", "temperature": 0.7, "timeout_s": 5, "stream": True}
        with mock.patch("urllib.request.urlopen", return_value=FakeResp(body)):
            msg, usage = agent._chat_stream(cfg, "m", [{"role": "user", "content": "hi"}], None,
                                            on_delta=deltas.append)
        self.assertEqual(msg["content"], "Hello world")
        self.assertEqual("".join(deltas), "Hello world")
        self.assertEqual(usage.get("prompt_tokens"), 10)

    def test_streams_tool_calls(self):
        body = _sse(
            {"choices": [{"delta": {"tool_calls": [{
                "index": 0, "id": "c1", "type": "function",
                "function": {"name": "list_dir", "arguments": ""},
            }]}}]},
            {"choices": [{"delta": {"tool_calls": [{
                "index": 0, "function": {"arguments": '{"path":"."}'},
            }]}}]},
            None,
        )
        cfg = {"base_url": "http://127.0.0.1:1234/v1", "temperature": 0.7, "timeout_s": 5}
        with mock.patch("urllib.request.urlopen", return_value=FakeResp(body)):
            msg, _usage = agent._chat_stream(cfg, "m", [], None)
        self.assertEqual(len(msg["tool_calls"]), 1)
        self.assertEqual(msg["tool_calls"][0]["function"]["name"], "list_dir")
        self.assertEqual(msg["tool_calls"][0]["function"]["arguments"], '{"path":"."}')

    def test_reasoning_content_fallback(self):
        body = _sse(
            {"choices": [{"delta": {"reasoning_content": "think…"}}]},
            {"choices": [{"delta": {"reasoning_content": " more"}}]},
            None,
        )
        cfg = {"base_url": "http://127.0.0.1:1234/v1", "temperature": 0.7, "timeout_s": 5}
        with mock.patch("urllib.request.urlopen", return_value=FakeResp(body)):
            msg, _usage = agent._chat_stream(cfg, "m", [], None)
        self.assertEqual(msg["content"], "think… more")

    def test_empty_stream_raises(self):
        cfg = {"base_url": "http://127.0.0.1:1234/v1", "temperature": 0.7, "timeout_s": 5}
        with mock.patch("urllib.request.urlopen", return_value=FakeResp(b"")):
            with self.assertRaises(agent.ServerError) as ctx:
                agent._chat_stream(cfg, "m", [], None)
        self.assertIn("Empty stream", str(ctx.exception))

    def test_stream_options_retry_on_400(self):
        cfg = {"base_url": "http://127.0.0.1:1234/v1", "temperature": 0.7, "timeout_s": 5, "stream": True}
        ok = _sse({"choices": [{"delta": {"content": "ok"}}]}, None)
        http_err = urllib.error.HTTPError(
            url="http://x/v1/chat/completions", code=400, msg="bad",
            hdrs=None, fp=io.BytesIO(b"nope"),
        )
        with mock.patch("urllib.request.urlopen", side_effect=[http_err, FakeResp(ok)]):
            msg, _usage = agent._chat_stream(cfg, "m", [], None)
        self.assertEqual(msg["content"], "ok")

    def test_chat_respects_stream_false(self):
        payload = {
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        cfg = {"base_url": "http://127.0.0.1:1234/v1", "temperature": 0.7, "timeout_s": 5, "stream": False}
        with mock.patch("urllib.request.urlopen", return_value=FakeResp(json.dumps(payload).encode())):
            msg, usage = agent._chat(cfg, "m", [], None)
        self.assertEqual(msg["content"], "ok")
        self.assertEqual(usage["total_tokens"], 2)


class ActIntegrationTests(unittest.TestCase):
    def test_act_silent_default_echoes_once_no_live_plain(self):
        body = _sse(
            {"choices": [{"delta": {"content": "**hello**"}}]},
            None,
        )
        cfg = {
            "base_url": "http://127.0.0.1:1234/v1",
            "temperature": 0.7,
            "timeout_s": 5,
            "stream": True,
            "confirm_shell": False,
        }
        echoed = []
        live = []
        messages = [{"role": "user", "content": "hi"}]
        with mock.patch("urllib.request.urlopen", return_value=FakeResp(body)), \
             mock.patch.object(agent, "_default_echo_delta", live.append), \
             mock.patch.object(agent, "_GeneratingIndicator") as ind_cls:
            ind = mock.Mock()
            ind_cls.return_value = ind
            agent.act(cfg, "m", messages, echo=echoed.append, echo_tool=lambda n, a: None)
        self.assertEqual(echoed, ["**hello**"])
        self.assertEqual(live, [])
        ind.tick.assert_called()
        ind.clear.assert_called_once()

    def test_act_live_plain_true_skips_echo(self):
        body = _sse(
            {"choices": [{"delta": {"content": "plain"}}]},
            None,
        )
        cfg = {
            "base_url": "http://127.0.0.1:1234/v1",
            "temperature": 0.7,
            "timeout_s": 5,
            "stream": True,
            "confirm_shell": False,
        }
        echoed = []
        out = []
        messages = [{"role": "user", "content": "hi"}]
        with mock.patch("urllib.request.urlopen", return_value=FakeResp(body)):
            agent.act(
                cfg, "m", messages,
                echo=echoed.append,
                echo_delta=out.append,
                echo_tool=lambda n, a: None,
            )
        self.assertEqual(echoed, [])
        self.assertEqual("".join(out), "plain\n")


if __name__ == "__main__":
    unittest.main()

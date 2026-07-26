"""Tests for streaming chat assembly (SSE + tool_call deltas)."""

import io
import json
import os
import unittest
from unittest import mock

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

    def test_erase_clears_tracked_output_on_tty(self):
        out, p = self._collect()
        p.feed("Hello")
        p.finish()
        with mock.patch("sys.stdout.isatty", return_value=True), \
             mock.patch("sys.stdout.write") as write, \
             mock.patch("sys.stdout.flush"), \
             mock.patch("lmloop.agent.shutil.get_terminal_size",
                        return_value=os.terminal_size((80, 24))):
            p.erase()
        written = "".join(c.args[0] for c in write.call_args_list)
        self.assertIn("\033[1A", written)
        self.assertIn("\033[J", written)
        self.assertEqual(p._written, [])


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


if __name__ == "__main__":
    unittest.main()

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lmloop import usage


class UsageRecordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "usage.jsonl"
        self.p_path = mock.patch.object(usage, "USAGE_PATH", self.path)
        self.p_path.start()
        self.addCleanup(self.p_path.stop)

    def test_record_appends_jsonl(self):
        usage.record("cli.command", command="memory")
        rows = usage.read_events(self.path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["feature"], "cli.command")
        self.assertEqual(rows[0]["detail"]["command"], "memory")
        self.assertIn("ts", rows[0])

    def test_disabled_via_env(self):
        with mock.patch.dict(os.environ, {"LMLOOP_USAGE": "0"}):
            usage.record("cli.command", command="memory")
        self.assertFalse(self.path.exists())

    def test_tracked_decorator(self):
        @usage.tracked("test.feature", tag="a")
        def fn():
            return 7

        self.assertEqual(fn(), 7)
        rows = usage.read_events(self.path)
        self.assertEqual(rows[0]["feature"], "test.feature")
        self.assertEqual(rows[0]["detail"]["tag"], "a")


class UsageIntegrationTests(unittest.TestCase):
    def test_dispatch_records_tool(self):
        from lmloop.tools import build_tools, dispatch

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "usage.jsonl"
            with mock.patch.object(usage, "USAGE_PATH", path):
                _specs, impls = build_tools({})
                dispatch(impls, "list_dir", '{"path": "."}')
            rows = usage.read_events(path)
            self.assertTrue(any(r.get("feature") == "tool" and r["detail"]["name"] == "list_dir"
                                for r in rows))

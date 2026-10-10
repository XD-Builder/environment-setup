"""Wave 1: parse cache, FTS index, run nodes, eval join, REPL mine route."""

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from lmloop import evals, knowledge_graph, memory, memory_index
from lmloop.config import DEFAULTS
from lmloop.repl import _MINE_LAST, _hud_line


def _cfg(**extra):
    cfg = dict(DEFAULTS)
    cfg.update(extra)
    return cfg


class ParseCacheTests(unittest.TestCase):
    def test_append_parses_tail_only(self):
        memory._JSONL_CACHE.clear()
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "rows.jsonl"
            path.write_text('{"n": 1, "pad": "' + ("x" * 5000) + '"}\n', encoding="utf-8")
            first = memory.read_jsonl(path)
            self.assertEqual(first[0]["n"], 1)
            with patch.object(Path, "read_bytes", side_effect=AssertionError("full read")):
                with path.open("a", encoding="utf-8") as handle:
                    handle.write('{"n": 2}\n')
                os.utime(path, None)
                rows = memory.read_jsonl(path)
            self.assertEqual([row["n"] for row in rows], [1, 2])

    def test_rewrite_rereads(self):
        memory._JSONL_CACHE.clear()
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "rows.jsonl"
            path.write_text('{"n": 1}\n', encoding="utf-8")
            memory.read_jsonl(path)
            path.write_text('{"n": 9}\n', encoding="utf-8")
            os.utime(path, None)
            self.assertEqual(memory.read_jsonl(path), [{"n": 9}])

    def test_corrupt_line_warns_once(self):
        memory._JSONL_CACHE.clear()
        memory._JSONL_WARNED.clear()
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "rows.jsonl"
            path.write_text('{"n": 1}\nnot-json\n', encoding="utf-8")
            with patch("sys.stderr") as err:
                rows = memory.read_jsonl(path)
                memory.read_jsonl(path)
            self.assertEqual(rows, [{"n": 1}])
            text = "".join(str(call.args[0]) for call in err.write.call_args_list)
            self.assertEqual(text.count("skipped corrupt"), 1)


class MemoryIndexTests(unittest.TestCase):
    def _root(self):
        return tempfile.TemporaryDirectory()

    def test_incremental_sync_and_query(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with patch.object(memory, "project_dir", return_value=root):
                memory.add_learning(
                    "see loop.py for the runner", type="pattern", key="runner",
                    source="observed", confidence=8,
                )
                (root / "sessions").mkdir()
                (root / "sessions" / "s.jsonl").write_text(
                    '{"role":"user","content":"remember loop.py","ts":"t"}\n'
                    '{"role":"tool","content":"hidden tool row"}\n',
                    encoding="utf-8",
                )
                cfg = _cfg(memory_index="auto", recall_sessions="on",
                           base_url="http://127.0.0.1:1234/v1")
                idx = memory_index.MemoryIndex()
                idx.reindex(cfg)
                self.assertEqual(idx.backend, "fts5")
                text = idx.search(
                    "loop.py", learning_limit=5, decision_limit=5, cfg=cfg,
                )
                self.assertIn("runner", text)
                self.assertIn("Past sessions", text)
                self.assertNotIn("hidden tool", text)
                self.assertEqual(
                    idx.search("it's", learning_limit=5, decision_limit=5, cfg=cfg),
                    "(no memory matches)",
                )
                self.assertEqual(
                    idx.search("", learning_limit=5, decision_limit=5, cfg=cfg),
                    "(no memory matches)",
                )
                self.assertEqual(
                    idx.search("the", learning_limit=5, decision_limit=5, cfg=cfg),
                    "(no memory matches)",
                )
                remote = _cfg(
                    memory_index="auto", recall_sessions="auto",
                    base_url="https://openrouter.ai/api/v1",
                )
                hidden = idx.search(
                    "loop.py", learning_limit=5, decision_limit=5, cfg=remote,
                )
                self.assertNotIn("Past sessions", hidden)
                learnings = root / "learnings.jsonl"
                before = learnings.read_text(encoding="utf-8")
                learnings.write_text(before + '{"ts":"t","type":"pattern","key":"partial",', encoding="utf-8")
                idx.sync(cfg)
                counts = idx.doc_counts()
                learnings.write_text(
                    before + '{"ts":"t","type":"pattern","key":"partial","insight":"done","confidence":5,"source":"observed"}\n',
                    encoding="utf-8",
                )
                idx.sync(cfg)
                self.assertGreaterEqual(idx.doc_counts().get("learning", 0), counts.get("learning", 0))

    def test_command_candidates_rank_and_match_scan(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with patch.object(memory, "project_dir", return_value=root):
                memory.add_learning(
                    "run `true`", type="tool", key="obs",
                    source="observed", confidence=10,
                )
                memory.add_learning(
                    "run `false`", type="operational", key="user",
                    source="user-stated", confidence=1,
                )
                memory.add_learning(
                    'skip `echo "unterminated`', type="tool", key="bad",
                    source="user-stated", confidence=9,
                )
                on = memory_index.MemoryIndex().command_candidates(_cfg(memory_index="auto"))
                off = memory_index.MemoryIndex().command_candidates(_cfg(memory_index="off"))
                self.assertEqual([row["cmd"] for row in on], ["false", "true"])
                self.assertEqual([row["cmd"] for row in off], ["false", "true"])

    def test_readonly_and_concurrent_sync(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with patch.object(memory, "project_dir", return_value=root):
                memory.add_learning("alpha beta", key="ab", source="observed")
                cfg = _cfg(memory_index="auto")
                idx = memory_index.MemoryIndex()
                idx.reindex(cfg)
                conn = idx.connect_readonly()
                with self.assertRaises(Exception):
                    conn.execute("INSERT INTO meta(k,v) VALUES('x','y')")
                    conn.commit()
                conn.close()
                errors = []

                def _sync():
                    try:
                        memory_index.MemoryIndex().sync(cfg)
                    except Exception as exc:  # pragma: no cover - surfaced below
                        errors.append(exc)

                threads = [threading.Thread(target=_sync) for _ in range(2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
                self.assertEqual(errors, [])


class RunNodeTests(unittest.TestCase):
    def test_off_writes_nothing_and_repeat_is_one_node(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with patch.object(memory, "project_dir", return_value=root):
                knowledge_graph.record_workflow_run(
                    _cfg(use_graph=False), run_key="r1", goal="ship",
                    commands=["python -m unittest"],
                )
                self.assertFalse((root / "graph_nodes.jsonl").exists())
                cfg = _cfg(use_graph=True)
                knowledge_graph.record_workflow_run(
                    cfg, run_key="r1", goal="ship", commands=["python -m unittest"],
                )
                knowledge_graph.record_workflow_run(
                    cfg, run_key="r1", goal="ship", commands=["python -m unittest"],
                )
                nodes = knowledge_graph.KnowledgeGraph().nodes()
                runs = [row for row in nodes.values() if row.get("type") == "run"]
                self.assertEqual(len(runs), 1)
                goals = [row for row in nodes.values() if row.get("type") == "goal"]
                self.assertEqual(len(goals), 1)


class EvalWaveTests(unittest.TestCase):
    def test_join_run_logs_and_healthy_fixture(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            until = root / "until"
            until.mkdir()
            secret = "API_KEY=super"
            (until / "a.jsonl").write_text(
                json.dumps({
                    "role": "check",
                    "results": [
                        {"cmd": "pytest -q", "status": "fail"},
                        {"cmd": secret, "status": "fail"},
                    ],
                }) + "\n" + json.dumps({
                    "role": "check",
                    "results": [
                        {"cmd": "pytest -q", "status": "fail"},
                        {"cmd": secret, "status": "fail"},
                    ],
                }) + "\n",
                encoding="utf-8",
            )
            graphs = root / "graphs" / "company"
            graphs.mkdir(parents=True)
            (graphs / "g.jsonl").write_text(
                json.dumps({"role": "node", "node": "qa", "status": "pause"}) + "\n"
                + json.dumps({"role": "pause", "node": "qa", "status": "paused"}) + "\n",
                encoding="utf-8",
            )
            with patch("lmloop.config.project_dir", return_value=root):
                stats = evals.EvalStats()
                stats.events = 4
                stats.tool_errors["run_shell"] = 3
                stats.until_outcomes["paused"] = 1
                evals.join_run_logs(stats, slug=None)
            self.assertIn("pytest -q", stats.never_pass_checks)
            self.assertNotIn(secret, stats.never_pass_checks)
            self.assertIn("qa", stats.always_pause_nodes)
            gaps = {g.id for g in evals.find_gaps(stats, min_samples=1)}
            self.assertIn("check-never-passes", gaps)
            self.assertIn("shell-error-before-pause", gaps)

        fixture = Path(__file__).resolve().parent / "fixtures" / "usage_healthy.jsonl"
        events = []
        for line in fixture.read_text(encoding="utf-8").splitlines():
            events.append(json.loads(line))
        stats = evals.aggregate(events)
        action = [g for g in evals.find_gaps(stats, min_samples=8) if g.severity == "action"]
        self.assertEqual(action, [])
        self.assertNotIn("cli-unused", {g.id for g in evals.find_gaps(stats, min_samples=8)})


class HudRouteTests(unittest.TestCase):
    def test_long_session_nudge_and_mine_route(self):
        class State:
            cfg = _cfg()
            messages = [{"role": "user"}] * 8

        self.assertIn("long session", _hud_line(State()))
        match = _MINE_LAST.match("mine last 3 sessions")
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.group(1), "3")
        self.assertIsNone(_MINE_LAST.match("please mine something"))


if __name__ == "__main__":
    unittest.main()

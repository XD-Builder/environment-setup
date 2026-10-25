"""Skipped-by-default memory hot-path timings (design task A1).

Run on demand::

    LMLOOP_MEMORY_PERF=1 PYTHONPATH=. .venv/bin/python -m unittest tests.test_memory_perf -v

The synthetic generator matches the shapes in DESIGN_MEMORY_RETRIEVAL Part 0.
The timed case is the regression lock: graph ``stats`` at 1,000 learnings
stays under a second. CI does not set the env var.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from lmloop.memory.knowledge_graph import KnowledgeGraph

PERF = os.environ.get("LMLOOP_MEMORY_PERF") == "1"


def write_synthetic(root: Path, *, learnings: int, decisions: int, edges: int) -> None:
    """Append-only JSONL the knowledge graph and memory readers already understand."""
    root.mkdir(parents=True, exist_ok=True)
    learning_rows = []
    for i in range(learnings):
        learning_rows.append({
            "ts": "2026-01-01T00:00:00Z",
            "type": "pattern",
            "key": f"learn-{i}",
            "insight": f"docker timeout note {i} about loop.py",
            "confidence": 7,
            "source": "user-stated",
        })
    decision_rows = []
    for i in range(decisions):
        decision_rows.append({
            "ts": "2026-01-01T00:00:00Z",
            "id": f"d{i}",
            "decision": f"keep check {i}",
            "rationale": "measured",
            "status": "active",
        })
    node_rows = [
        {"ts": "2026-01-01T00:00:00Z", "type": "learning", "key": f"learn-{i}",
         "label": f"learn {i}", "confidence": 7, "source": "user-stated"}
        for i in range(learnings)
    ]
    node_rows.extend(
        {"ts": "2026-01-01T00:00:00Z", "type": "decision", "key": f"d{i}",
         "label": f"keep check {i}", "confidence": 7, "source": "user-stated"}
        for i in range(decisions)
    )
    edge_rows = []
    for i in range(edges):
        src = i % max(learnings, 1)
        dst = (i + 1) % max(learnings, 1)
        edge_rows.append({
            "ts": "2026-01-01T00:00:00Z",
            "from_type": "learning",
            "from_key": f"learn-{src}",
            "to_type": "learning",
            "to_key": f"learn-{dst}",
            "edge_type": "related_to",
        })
    _dump(root / "learnings.jsonl", learning_rows)
    _dump(root / "decisions.jsonl", decision_rows)
    _dump(root / "graph_nodes.jsonl", node_rows)
    _dump(root / "graph_edges.jsonl", edge_rows)


def _dump(path: Path, rows: list) -> None:
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )


def _time_stats(root: Path) -> float:
    with mock.patch("lmloop.memory.project_dir", return_value=root):
        started = time.perf_counter()
        text = KnowledgeGraph().stats()
        elapsed = time.perf_counter() - started
    if "nodes:" not in text:
        raise AssertionError(text[:200])
    return elapsed


class SyntheticShapeTests(unittest.TestCase):
    def test_generator_writes_the_requested_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_synthetic(root, learnings=4, decisions=2, edges=3)
            self.assertEqual(len((root / "learnings.jsonl").read_text().splitlines()), 4)
            self.assertEqual(len((root / "decisions.jsonl").read_text().splitlines()), 2)
            self.assertEqual(len((root / "graph_edges.jsonl").read_text().splitlines()), 3)
            with mock.patch("lmloop.memory.project_dir", return_value=root):
                text = KnowledgeGraph().stats()
            self.assertIn("nodes: 6", text)
            self.assertIn("edges: 3", text)


@unittest.skipUnless(PERF, "set LMLOOP_MEMORY_PERF=1 to print Part 0 timings")
class MemoryPerfTests(unittest.TestCase):
    def test_part0_tables(self):
        rows = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for learnings, decisions, edges in (
                (200, 50, 500),
                (1000, 200, 3000),
            ):
                write_synthetic(root, learnings=learnings, decisions=decisions, edges=edges)
                elapsed = _time_stats(root)
                rows.append((learnings, decisions, edges, elapsed))
        print("\nlearnings  decisions  edges  stats_s")
        for learnings, decisions, edges, elapsed in rows:
            print(f"{learnings:9}  {decisions:9}  {edges:5}  {elapsed:.4f}")
        slow = rows[-1][3]
        self.assertLess(slow, 1.0, f"stats at 1,000 learnings took {slow:.3f}s")


if __name__ == "__main__":
    unittest.main()

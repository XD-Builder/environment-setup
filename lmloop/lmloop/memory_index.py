"""Derived memory index (FTS5 when available, else scan). Leaf-adjacent module."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import memory
from .config import cfg_str, project_dir

INDEX_VERSION = 1
INDEX_FILE = "index.sqlite3"


def _mode(cfg: dict) -> str:
    return cfg_str(cfg, "memory_index").strip().lower()


def index_path(slug: "str | None" = None) -> Path:
    return project_dir(slug) / INDEX_FILE


def _fts5_available() -> bool:
    try:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        conn.close()
        return True
    except sqlite3.OperationalError:
        return False


class MemoryIndex:
    def __init__(self, slug: "str | None" = None):
        self.slug = slug
        self.path = index_path(slug)
        self.backend = "scan"

    def open(self, cfg: dict) -> "MemoryIndex":
        mode = _mode(cfg)
        if mode == "off":
            self.backend = "off"
            return self
        if mode == "on" and not _fts5_available():
            raise RuntimeError("memory_index on but FTS5 is unavailable in this Python")
        if mode == "auto" and _fts5_available():
            self.backend = "fts5"
            self._ensure_schema()
        else:
            self.backend = "scan"
        return self

    def _ensure_schema(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=2000")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)"
            )
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5("
                "kind, key, title, body, tokenize='porter unicode61')"
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta(k,v) VALUES('schema_version', ?)",
                (str(INDEX_VERSION),),
            )
            conn.commit()
        finally:
            conn.close()

    def sync(self, cfg: dict) -> None:
        if self.backend != "fts5":
            return
        conn = sqlite3.connect(self.path)
        try:
            conn.execute("DELETE FROM docs")
            for row in memory.get_learnings(limit=None, slug=self.slug):
                conn.execute(
                    "INSERT INTO docs(kind,key,title,body) VALUES(?,?,?,?)",
                    ("learning", row.get("key", ""), row.get("key", ""),
                     row.get("insight", "")),
                )
            for row in memory.get_decisions(limit=None, slug=self.slug):
                body = (row.get("decision") or "") + "\n" + (row.get("rationale") or "")
                conn.execute(
                    "INSERT INTO docs(kind,key,title,body) VALUES(?,?,?,?)",
                    ("decision", row.get("id", ""), row.get("decision", ""), body),
                )
            conn.commit()
        finally:
            conn.close()

    def search(self, query: str, *, learning_limit: int, decision_limit: int,
               cfg: dict) -> str:
        if cfg and cfg.get("use_graph"):
            return memory._search_memory_scan(
                query, learning_limit, decision_limit, self.slug, cfg,
            )
        self.open(cfg)
        if self.backend in ("off", "scan"):
            return memory._search_memory_scan(
                query, learning_limit, decision_limit, self.slug, cfg,
            )
        self.sync(cfg)
        conn = sqlite3.connect(self.path)
        try:
            rows = conn.execute(
                "SELECT kind, key, title, body FROM docs WHERE docs MATCH ? LIMIT ?",
                (query, learning_limit + decision_limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return memory._search_memory_scan(
                query, learning_limit, decision_limit, self.slug, cfg,
            )
        finally:
            conn.close()
        learnings, decisions = [], []
        for kind, key, title, body in rows:
            if kind == "learning" and len(learnings) < learning_limit:
                learnings.append(f"- [{key}] {body}")
            elif kind == "decision" and len(decisions) < decision_limit:
                decisions.append(f"- [{key}] {title}")
        out = []
        if learnings:
            out.append("Learnings:\n" + "\n".join(learnings))
        if decisions:
            out.append("Decisions:\n" + "\n".join(decisions))
        return "\n\n".join(out) or "(no memory matches)"

    def reindex(self, cfg: dict) -> None:
        """Delete and rebuild the FTS5 index from JSONL sources."""
        self.open(cfg)
        if self.backend != "fts5":
            return
        if self.path.is_file():
            self.path.unlink()
        self._ensure_schema()
        self.sync(cfg)

    def doc_counts(self) -> dict:
        if not self.path.is_file() or self.backend != "fts5":
            learn = len(memory.get_learnings(limit=None, slug=self.slug))
            dec = len(memory.get_decisions(limit=None, slug=self.slug))
            return {"learning": learn, "decision": dec}
        conn = sqlite3.connect(self.path)
        try:
            rows = conn.execute(
                "SELECT kind, COUNT(*) FROM docs GROUP BY kind",
            ).fetchall()
            return {k: int(c) for k, c in rows}
        finally:
            conn.close()

    def status(self, cfg: dict) -> dict:
        self.open(cfg)
        size = self.path.stat().st_size if self.path.is_file() else 0
        reason = ""
        if _mode(cfg) == "off":
            reason = "memory_index off"
        elif self.backend == "scan":
            reason = "FTS5 unavailable or memory_index auto without FTS5"
        counts = self.doc_counts()
        total = sum(counts.values())
        return {
            "backend": self.backend,
            "path": str(self.path),
            "size": size,
            "reason": reason,
            "counts": counts,
            "docs": total,
        }


def index_status_line(cfg: dict, slug: "str | None" = None) -> str:
    """One-line index summary for ``/stats`` and ``memory index``."""
    st = MemoryIndex(slug).status(cfg)
    backend = st["backend"]
    if backend == "off":
        return "memory index: off"
    if backend == "scan":
        tail = st["reason"] or "scan"
        return f"memory index: scan ({tail}) · {st['docs']} docs"
    size_mb = st["size"] / (1024 * 1024)
    return (
        f"memory index: fts5 · {size_mb:.1f} MB · {st['docs']} docs"
    )


def format_index_report(cfg: dict, slug: "str | None" = None) -> str:
    st = MemoryIndex(slug).status(cfg)
    lines = [index_status_line(cfg, slug), f"path: {st['path']}"]
    if st.get("reason"):
        lines.append(f"note: {st['reason']}")
    counts = st.get("counts") or {}
    if counts:
        parts = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        lines.append(f"docs by kind: {parts}")
    return "\n".join(lines)

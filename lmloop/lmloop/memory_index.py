"""Derived memory index (FTS5 when available, else scan). Leaf-adjacent module.

JSONL stays the source of truth. ``index.sqlite3`` is disposable: any failure
deletes it and the process falls back to the scan path.
"""

from __future__ import annotations

import hashlib
import json
import shlex
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlparse

from . import memory
from .config import cfg_bool, cfg_str

INDEX_VERSION = 2
INDEX_FILE = "index.sqlite3"
_BUSY_MS = 2000
_SWEEP_S = 60
_PREFIX_BYTES = 4096
_SNIPPET_CAP = 300
_SNIPPET_MAX = 3
_FTS_LIMIT = 200

_SCHEMA = """
CREATE TABLE sources (
  path TEXT PRIMARY KEY,
  inode INTEGER,
  size INTEGER,
  mtime_ns INTEGER,
  offset INTEGER,
  prefix_sha TEXT
);
CREATE TABLE docs (
  id INTEGER PRIMARY KEY,
  kind TEXT,
  key TEXT,
  source TEXT,
  line INTEGER,
  ts TEXT,
  confidence REAL,
  src TEXT,
  active INTEGER,
  title TEXT,
  body TEXT,
  subtype TEXT
);
CREATE INDEX docs_kind_key ON docs(kind, key);
CREATE VIRTUAL TABLE docs_fts USING fts5(
  title, body, content='docs', content_rowid='id',
  tokenize='porter unicode61'
);
CREATE VIRTUAL TABLE docs_ident USING fts5(
  title, body, content='docs', content_rowid='id',
  tokenize="unicode61 tokenchars '_./-'"
);
CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TRIGGER docs_ai AFTER INSERT ON docs BEGIN
  INSERT INTO docs_fts(rowid, title, body) VALUES (new.id, new.title, new.body);
  INSERT INTO docs_ident(rowid, title, body) VALUES (new.id, new.title, new.body);
END;
CREATE TRIGGER docs_ad AFTER DELETE ON docs BEGIN
  INSERT INTO docs_fts(docs_fts, rowid, title, body)
    VALUES('delete', old.id, old.title, old.body);
  INSERT INTO docs_ident(docs_ident, rowid, title, body)
    VALUES('delete', old.id, old.title, old.body);
END;
"""


def _mode(cfg: dict) -> str:
    return cfg_str(cfg, "memory_index").strip().lower()


def index_path(slug: "str | None" = None) -> Path:
    return memory.project_dir(slug) / INDEX_FILE


def _fts5_available() -> bool:
    try:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        conn.close()
        return True
    except sqlite3.OperationalError:
        return False


def _prefix_sha(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as handle:
        digest.update(handle.read(_PREFIX_BYTES))
    return digest.hexdigest()


def _quote_token(token: str) -> str:
    return '"' + token.replace('"', '""') + '"'


def sessions_enabled(cfg: dict) -> bool:
    """Whether transcript snippets may enter a prompt (R-PRIV)."""
    mode = cfg_str(cfg, "recall_sessions").strip().lower()
    if mode == "off":
        return False
    if mode == "on":
        return True
    from .server import _is_local_host

    host = urlparse(cfg_str(cfg, "base_url")).hostname or ""
    return _is_local_host(host)


def _backtick_cmds(text: str) -> list[str]:
    spans: list[str] = []
    i = 0
    raw = text or ""
    while True:
        start = raw.find("`", i)
        if start < 0:
            break
        end = raw.find("`", start + 1)
        if end < 0:
            break
        spans.append(raw[start + 1:end])
        i = end + 1
    out: list[str] = []
    for cmd in spans:
        cmd = cmd.strip()
        if not cmd:
            continue
        try:
            shlex.split(cmd, posix=True)
        except ValueError:
            continue
        out.append(cmd)
    return out


class MemoryIndex:
    def __init__(self, slug: "str | None" = None):
        self.slug = slug
        self.path = index_path(slug)
        self.backend = "scan"
        self.reason = ""

    def open(self, cfg: dict) -> "MemoryIndex":
        mode = _mode(cfg)
        if mode == "off":
            self.backend = "off"
            self.reason = "memory_index off"
            return self
        if not _fts5_available():
            if mode == "on":
                raise RuntimeError("memory_index on but FTS5 is unavailable in this Python")
            self.backend = "scan"
            self.reason = "FTS5 unavailable"
            return self
        try:
            self._ensure_schema()
        except sqlite3.Error as exc:
            self._fall_back(f"index open failed: {exc}")
            return self
        if self.backend != "fts5":
            return self
        return self

    def _fall_back(self, reason: str) -> None:
        self.backend = "scan"
        self.reason = reason
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(self.path) + suffix) if suffix else self.path
            try:
                if path.is_file():
                    path.unlink()
            except OSError:
                pass

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=_BUSY_MS / 1000)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={_BUSY_MS}")
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            raise sqlite3.OperationalError(f"WAL refused ({mode})")
        return conn

    def _ensure_schema(self) -> None:
        if self.path.is_file():
            try:
                conn = self._connect()
            except sqlite3.Error as exc:
                self._fall_back(f"could not open index: {exc}")
                return
            try:
                version = conn.execute(
                    "SELECT v FROM meta WHERE k='schema_version'",
                ).fetchone()
                if version and version[0] == str(INDEX_VERSION):
                    self.backend = "fts5"
                    return
            except sqlite3.Error:
                pass
            finally:
                conn.close()
            self._unlink_index()
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
            conn.execute(
                "INSERT INTO meta(k,v) VALUES('schema_version', ?)",
                (str(INDEX_VERSION),),
            )
            conn.commit()
            self.backend = "fts5"
            self.reason = ""
        except sqlite3.Error as exc:
            self._fall_back(str(exc))
        finally:
            conn.close()

    def _unlink_index(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(self.path) + suffix) if suffix else self.path
            try:
                if path.is_file():
                    path.unlink()
            except OSError:
                pass

    def connect_readonly(self) -> sqlite3.Connection:
        """Read-only connection for the canvas. Never writes."""
        uri = self.path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=_BUSY_MS / 1000)
        conn.execute(f"PRAGMA busy_timeout={_BUSY_MS}")
        return conn

    def sync(self, cfg: dict, *, force: bool = False) -> None:
        if self.backend != "fts5":
            return
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            now = time.time()
            last = conn.execute(
                "SELECT v FROM meta WHERE k='last_sweep'",
            ).fetchone()
            last_ts = float(last[0]) if last else 0.0
            full = force or (now - last_ts) >= _SWEEP_S
            root = memory.project_dir(self.slug)
            paths = self._list_sources(conn, root, full=full)
            known = {
                row["path"]: row
                for row in conn.execute("SELECT * FROM sources")
            }
            seen: set[str] = set()
            for path in paths:
                key = str(path)
                seen.add(key)
                if not path.is_file():
                    self._drop_source(conn, key)
                    continue
                st = path.stat()
                prev = known.get(key)
                prefix = _prefix_sha(path)
                stored = int(prev["size"]) if prev else -1
                if (
                    prev
                    and int(prev["inode"]) == st.st_ino
                    and stored == st.st_size
                    and int(prev["mtime_ns"]) == st.st_mtime_ns
                    and prev["prefix_sha"] == prefix
                ):
                    continue
                if (
                    prev
                    and int(prev["inode"]) == st.st_ino
                    and st.st_size > stored
                    and prev["prefix_sha"] == prefix
                    and path.suffix != ".md"
                ):
                    self._ingest_tail(conn, path, int(prev["offset"] or 0))
                else:
                    self._drop_source(conn, key)
                    self._ingest_tail(conn, path, 0)
            if full:
                for key in list(known):
                    if key not in seen:
                        self._drop_source(conn, key)
                conn.execute(
                    "INSERT OR REPLACE INTO meta(k,v) VALUES('last_sweep', ?)",
                    (str(now),),
                )
            self._recompute_active(conn)
            conn.execute(
                "INSERT OR REPLACE INTO meta(k,v) VALUES('last_sync', ?)",
                (str(now),),
            )
            conn.commit()
        except sqlite3.Error as exc:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            self._fall_back(str(exc))
        finally:
            conn.close()

    def _list_sources(self, conn: sqlite3.Connection, root: Path, *, full: bool) -> list[Path]:
        found: list[Path] = []
        for name in ("learnings.jsonl", "decisions.jsonl"):
            path = root / name
            if path.is_file():
                found.append(path)
        dir_mtimes = {}
        raw = conn.execute(
            "SELECT v FROM meta WHERE k='dir_mtimes'",
        ).fetchone()
        if raw:
            try:
                dir_mtimes = json.loads(raw[0])
            except json.JSONDecodeError:
                dir_mtimes = {}
        updated = dict(dir_mtimes)
        for folder, pattern in (
            (root / "checkpoints", "*.md"),
            (root / "sessions", "*.jsonl"),
            (root / "until", "*.jsonl"),
            (root / "graphs", "*/*.jsonl"),
        ):
            key = str(folder)
            if not folder.is_dir():
                continue
            mtime = folder.stat().st_mtime_ns
            relist = full or dir_mtimes.get(key) != mtime
            updated[key] = mtime
            if relist:
                found.extend(sorted(folder.glob(pattern)))
            else:
                for row in conn.execute(
                    "SELECT path FROM sources WHERE path LIKE ?",
                    (key + "/%",),
                ):
                    found.append(Path(row[0]))
        conn.execute(
            "INSERT OR REPLACE INTO meta(k,v) VALUES('dir_mtimes', ?)",
            (json.dumps(updated),),
        )
        return found

    def _drop_source(self, conn: sqlite3.Connection, source: str) -> None:
        conn.execute("DELETE FROM docs WHERE source=?", (source,))
        conn.execute("DELETE FROM sources WHERE path=?", (source,))

    def _ingest_tail(self, conn: sqlite3.Connection, path: Path, offset: int) -> None:
        data = path.read_bytes()
        chunk = data[offset:]
        if path.suffix == ".md":
            text = data.decode("utf-8", errors="replace")
            self._insert_doc(
                conn, kind="checkpoint", key=path.stem, source=str(path),
                line=1, ts="", confidence=0, src="", active=1,
                title=path.stem, body=text[:8000], subtype="checkpoint",
            )
            new_offset = len(data)
        else:
            nl = chunk.rfind(b"\n")
            if nl < 0:
                return
            complete = chunk[: nl + 1]
            text = complete.decode("utf-8", errors="replace")
            start_line = 0
            if offset:
                row = conn.execute(
                    "SELECT MAX(line) FROM docs WHERE source=?", (str(path),),
                ).fetchone()
                start_line = int(row[0] or 0)
            self._ingest_jsonl_text(conn, path, text, start_line)
            new_offset = offset + len(complete)
        st = path.stat()
        conn.execute(
            "INSERT OR REPLACE INTO sources(path, inode, size, mtime_ns, offset, prefix_sha)"
            " VALUES(?,?,?,?,?,?)",
            (str(path), st.st_ino, new_offset, st.st_mtime_ns, new_offset, _prefix_sha(path)),
        )

    def _ingest_jsonl_text(
        self, conn: sqlite3.Connection, path: Path, text: str, start_line: int,
    ) -> None:
        name = path.name
        for idx, line in enumerate(text.splitlines(), start=start_line + 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            if name == "learnings.jsonl":
                self._insert_doc(
                    conn, kind="learning", key=str(row.get("key") or ""),
                    source=str(path), line=idx, ts=str(row.get("ts") or ""),
                    confidence=float(row.get("confidence") or 0),
                    src=str(row.get("source") or ""), active=0,
                    title=str(row.get("key") or ""),
                    body=str(row.get("insight") or ""),
                    subtype=str(row.get("type") or ""),
                )
            elif name == "decisions.jsonl":
                if row.get("kind") == "supersede" and not row.get("decision"):
                    continue
                body = (row.get("decision") or "") + "\n" + (row.get("rationale") or "")
                self._insert_doc(
                    conn, kind="decision", key=str(row.get("id") or ""),
                    source=str(path), line=idx, ts=str(row.get("date") or ""),
                    confidence=0, src="", active=0,
                    title=str(row.get("decision") or ""), body=body, subtype="decision",
                )
            elif "sessions" in path.parts:
                role = row.get("role") or ""
                if role not in ("user", "assistant"):
                    continue
                content = row.get("content")
                if not isinstance(content, str) or not content.strip():
                    continue
                self._insert_doc(
                    conn, kind="turn", key=f"{path.stem}:{idx}",
                    source=str(path), line=idx, ts=str(row.get("ts") or ""),
                    confidence=0, src=role, active=1,
                    title=path.stem, body=content[:4000], subtype="turn",
                )
            else:
                role = row.get("role") or ""
                if role in ("maker", "node") and (row.get("handoff") or ""):
                    self._insert_doc(
                        conn, kind="handoff", key=f"{path.stem}:{idx}",
                        source=str(path), line=idx, ts=str(row.get("ts") or ""),
                        confidence=0, src=role, active=1,
                        title=role, body=str(row.get("handoff")), subtype="summary",
                    )
                if role in ("check", "baseline") and row.get("results"):
                    self._insert_doc(
                        conn, kind="handoff", key=f"{path.stem}:check:{idx}",
                        source=str(path), line=idx, ts=str(row.get("ts") or ""),
                        confidence=0, src=role, active=1,
                        title="check",
                        body=json.dumps(row.get("results"), ensure_ascii=False),
                        subtype="results",
                    )

    def _insert_doc(self, conn, **fields) -> None:
        cols = (
            "kind", "key", "source", "line", "ts", "confidence", "src",
            "active", "title", "body", "subtype",
        )
        conn.execute(
            f"INSERT INTO docs({','.join(cols)}) VALUES({','.join('?' for _ in cols)})",
            tuple(fields[c] for c in cols),
        )

    def _recompute_active(self, conn: sqlite3.Connection) -> None:
        conn.execute("UPDATE docs SET active=0 WHERE kind='learning'")
        rows = conn.execute(
            "SELECT source, key, MAX(line) AS line FROM docs "
            "WHERE kind='learning' GROUP BY source, key",
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE docs SET active=1 WHERE kind='learning' AND source=? "
                "AND key=? AND line=?",
                (row["source"], row["key"], row["line"]),
            )
        active_ids = {
            item.get("id")
            for item in memory.get_decisions(limit=None, slug=self.slug)
        }
        conn.execute("UPDATE docs SET active=0 WHERE kind='decision'")
        for ident in active_ids:
            if ident:
                conn.execute(
                    "UPDATE docs SET active=1 WHERE kind='decision' AND key=?",
                    (ident,),
                )

    def search(self, query: str, *, learning_limit: int, decision_limit: int,
               cfg: dict) -> str:
        self.open(cfg)
        if self.backend in ("off", "scan"):
            base = memory._search_memory_scan(
                query, learning_limit, decision_limit, self.slug, cfg,
            )
            return base
        self.sync(cfg)
        if self.backend != "fts5":
            return memory._search_memory_scan(
                query, learning_limit, decision_limit, self.slug, cfg,
            )
        words, idents = memory.tokenize_query(query)
        tokens = words + idents
        if not tokens:
            return "(no memory matches)"
        match = " OR ".join(_quote_token(tok) for tok in tokens)
        try:
            conn = self._connect()
        except sqlite3.Error:
            return memory._search_memory_scan(
                query, learning_limit, decision_limit, self.slug, cfg,
            )
        try:
            try:
                ranked = conn.execute(
                    "SELECT d.id, d.kind, d.key, d.title, d.body, d.ts, d.source, d.src, "
                    "d.confidence, d.active, bm25(docs_fts, 2.0, 1.0) AS rank "
                    "FROM docs_fts JOIN docs d ON d.id = docs_fts.rowid "
                    "WHERE docs_fts MATCH ? AND d.active=1 "
                    "ORDER BY rank LIMIT ?",
                    (match, _FTS_LIMIT),
                ).fetchall()
            except sqlite3.OperationalError:
                return memory._search_memory_scan(
                    query, learning_limit, decision_limit, self.slug, cfg,
                )
            ident_ids: set[int] = set()
            if idents:
                ident_match = " OR ".join(_quote_token(tok) for tok in idents)
                try:
                    for row in conn.execute(
                        "SELECT rowid FROM docs_ident WHERE docs_ident MATCH ? LIMIT ?",
                        (ident_match, _FTS_LIMIT),
                    ):
                        ident_ids.add(int(row[0]))
                except sqlite3.OperationalError:
                    ident_ids = set()
        finally:
            conn.close()

        ranked = sorted(
            ranked,
            key=lambda row: (0 if int(row["id"]) in ident_ids else 1, row["rank"]),
        )
        learnings = []
        decisions = []
        snippets = []
        learning_rows = {
            r.get("key"): r
            for r in memory.get_learnings(limit=None, slug=self.slug)
        }
        decision_rows = {
            r.get("id"): r
            for r in memory.get_decisions(limit=None, slug=self.slug)
        }
        for row in ranked:
            kind = row["kind"]
            if kind == "learning" and len(learnings) < learning_limit:
                item = learning_rows.get(row["key"])
                if item is None:
                    continue
                if memory._effective_confidence(item) <= 0:
                    continue
                learnings.append(item)
            elif kind == "decision" and len(decisions) < decision_limit:
                item = decision_rows.get(row["key"])
                if item is not None:
                    decisions.append(item)
            elif kind in ("turn", "handoff", "checkpoint") and sessions_enabled(cfg):
                if len(snippets) < _SNIPPET_MAX:
                    body = (row["body"] or "").replace("\n", " ")
                    snippets.append(
                        f"- [{Path(row['source']).name} {row['ts']}] {body[:_SNIPPET_CAP]}"
                    )
        out = []
        if cfg_bool(cfg, "use_graph"):
            from .knowledge_graph import KnowledgeGraph, _node_id

            kg = KnowledgeGraph(self.slug)
            kg.ensure(cfg)
            view = kg.load_view()
        else:
            kg = None
            view = None
        if learnings:
            lines = []
            for item in learnings:
                lines.append(memory.format_learning_line(item))
                if kg is not None and view is not None:
                    lines.extend(kg.neighbor_lines(
                        _node_id("learning", item["key"]), view,
                    ))
            out.append("Learnings:\n" + "\n".join(lines))
        if decisions:
            lines = []
            for item in decisions:
                lines.append(memory.format_decision_line(item))
                if kg is not None and view is not None:
                    lines.extend(kg.neighbor_lines(
                        _node_id("decision", item["id"]), view,
                    ))
            out.append("Decisions:\n" + "\n".join(lines))
        if snippets:
            out.append("Past sessions:\n" + "\n".join(snippets))
        return "\n\n".join(out) or "(no memory matches)"

    def reindex(self, cfg: dict) -> None:
        """Delete and rebuild ``index.sqlite3`` from JSONL sources."""
        self.open(cfg)
        if self.backend != "fts5":
            return
        self._unlink_index()
        self._ensure_schema()
        if self.backend == "fts5":
            self.sync(cfg, force=True)

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
        reason = self.reason
        if _mode(cfg) == "off":
            reason = "memory_index off"
        elif self.backend == "scan" and not reason:
            reason = "FTS5 unavailable or memory_index auto without FTS5"
        counts = self.doc_counts()
        total = sum(counts.values())
        last_sync = ""
        if self.backend == "fts5" and self.path.is_file():
            try:
                conn = sqlite3.connect(self.path)
                row = conn.execute(
                    "SELECT v FROM meta WHERE k='last_sync'",
                ).fetchone()
                conn.close()
                if row:
                    last_sync = row[0]
            except sqlite3.Error:
                last_sync = ""
        return {
            "backend": self.backend,
            "path": str(self.path),
            "size": size,
            "reason": reason,
            "counts": counts,
            "docs": total,
            "last_sync": last_sync,
        }

    def command_candidates(self, cfg: dict) -> list[dict]:
        """Backticked tool/operational learnings, user-stated first.

        Scan fallback reads JSONL directly so inference does not require FTS5.
        """
        self.open(cfg)
        rows: list[dict] = []
        if self.backend == "fts5":
            self.sync(cfg)
            if self.backend == "fts5" and self.path.is_file():
                conn = sqlite3.connect(self.path)
                conn.row_factory = sqlite3.Row
                try:
                    fetched = conn.execute(
                        "SELECT key, body, src, confidence, subtype, ts FROM docs "
                        "WHERE kind='learning' AND active=1 "
                        "AND subtype IN ('tool', 'operational')",
                    ).fetchall()
                finally:
                    conn.close()
                for item in fetched:
                    rows.append({
                        "key": item["key"],
                        "insight": item["body"],
                        "source": item["src"],
                        "confidence": item["confidence"],
                        "type": item["subtype"],
                        "ts": item["ts"],
                    })
        if not rows:
            for item in memory.get_learnings(limit=None, slug=self.slug):
                if item.get("type") in ("tool", "operational"):
                    rows.append(item)
        found: list[dict] = []
        for item in rows:
            if memory._effective_confidence(item) <= 0:
                continue
            for cmd in _backtick_cmds(item.get("insight") or ""):
                found.append({
                    "cmd": cmd,
                    "source": item.get("source") or "observed",
                    "confidence": memory._effective_confidence(item),
                })
        found.sort(key=lambda row: (
            0 if row["source"] == "user-stated" else 1,
            -float(row["confidence"]),
            row["cmd"],
        ))
        return found


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
    if st.get("last_sync"):
        lines.append(f"last sync: {st['last_sync']}")
    counts = st.get("counts") or {}
    if counts:
        parts = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        lines.append(f"docs by kind: {parts}")
    return "\n".join(lines)

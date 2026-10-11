"""Opt-in JSONL knowledge graph: nodes, edges, recall hops.

Learnings, decisions, sessions, and checkpoints stay in ``memory.py``.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path

from . import memory
from .config import cfg_bool, utc_now

GRAPH_NODE_TYPES = frozenset({
    "learning", "decision", "session", "file", "skill", "concept",
    "run", "goal",
    "action", "thought", "trait", "self_snapshot",
})
GRAPH_EDGE_TYPES = frozenset({
    "leads_to", "contradicts", "in_session", "references",
    "uses_skill", "related_to", "supersedes",
    "evidence_for", "supports", "expresses", "grounded_in",
})
MSG_GRAPH_OFF = "knowledge graph is off — `lmloop config set use_graph true`"
CANVAS_NODE_CAP = 2000
_TYPE_BAND = {
    "session": 0.0,
    "learning": 1.0,
    "decision": 2.0,
    "file": 3.0,
    "skill": 4.0,
    "concept": 5.0,
    "run": 6.0,
    "goal": 7.0,
    "action": 8.0,
    "thought": 9.0,
    "trait": 10.0,
    "self_snapshot": 11.0,
}

# Per-project fingerprint: skip _backfill when learnings/decisions/sessions unchanged.
_BACKFILL_FP: dict[str, tuple] = {}


def _node_id(typ: str, key: str) -> tuple:
    return (typ, key)


def _paths_in_text(text: str) -> list:
    """Workspace-relative path tokens. Whitespace split, pathlib for shape."""
    found: list[str] = []
    seen: set[str] = set()
    for tok in (text or "").split():
        tok = tok.strip(".,;:()[]\"'`")
        if not tok or "://" in tok:
            continue
        p = Path(tok)
        if p.is_absolute() or tok.startswith("~"):
            continue
        suffix = p.suffix[1:] if p.suffix.startswith(".") else ""
        if "/" not in tok and not (suffix and suffix.isalnum()):
            continue
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        found.append(key)
    return found


def _session_key() -> str:
    if memory._ACTIVE_SESSION is None:
        return ""
    return memory._ACTIVE_SESSION.stem


def _graph_node_live(row: dict) -> bool:
    typ = row.get("type")
    if typ in ("learning", "concept"):
        return memory._effective_confidence(row) > 0
    return True


@dataclass(frozen=True)
class GraphView:
    """One scan of nodes/edges plus adjacency for a single operation."""

    nodes: dict
    edges: list
    adjacency: dict


@dataclass(frozen=True)
class CanvasNode:
    id: str
    type: str
    label: str
    detail: str
    confidence: int
    ts: str
    degree: int
    x: float
    y: float


@dataclass(frozen=True)
class CanvasView:
    nodes: tuple
    edges: tuple
    truncated: bool
    total_nodes: int


@dataclass
class KnowledgeGraph:
    """Opt-in JSONL knowledge graph for one project slug."""

    slug: "str | None" = None

    def nodes_path(self) -> Path:
        return memory.project_dir(self.slug) / "graph_nodes.jsonl"

    def edges_path(self) -> Path:
        return memory.project_dir(self.slug) / "graph_edges.jsonl"

    @staticmethod
    def enabled(cfg: "dict | None") -> bool:
        return bool(cfg and cfg_bool(cfg, "use_graph"))

    def ensure(self, cfg: dict) -> None:
        if not self.enabled(cfg):
            return
        root = memory.project_dir(self.slug)
        fp = self._source_fingerprint(root)
        cache_key = str(root)
        if _BACKFILL_FP.get(cache_key) == fp:
            return
        self._backfill()
        _BACKFILL_FP[cache_key] = fp

    def load_view(self) -> GraphView:
        """Latest nodes/edges and adjacency from one read of each JSONL file."""
        live = self.nodes()
        edge_rows = self._live_edges(live)
        return GraphView(live, edge_rows, self._adjacency(edge_rows))

    def canvas_view(
        self,
        query: str = "",
        types: "tuple[str, ...]" = (),
        limit: int = CANVAS_NODE_CAP,
    ) -> CanvasView:
        """Filter, cap, and lay out nodes for the terminal canvas (pure function)."""
        view = self.load_view()
        terms = [t.lower() for t in memory._query_terms(query)]
        type_filter = frozenset(types) if types else None
        candidates: list[tuple] = []
        for ident, row in view.nodes.items():
            typ = row.get("type") or ""
            if type_filter and typ not in type_filter:
                continue
            label = (row.get("label") or row.get("key") or "").strip()
            detail = label
            if typ == "learning":
                detail = row.get("label") or row.get("insight") or label
            hay = f"{row.get('key', '')} {label} {detail}".lower()
            if terms and not any(t in hay for t in terms):
                continue
            degree = len(view.adjacency.get(ident, []))
            candidates.append((ident, row, degree, detail))
        candidates.sort(key=lambda item: (item[0][0], item[0][1]))
        total = len(candidates)
        truncated = total > limit
        if truncated:
            candidates = candidates[:limit]
        by_band: dict[float, list] = {}
        for ident, row, degree, detail in candidates:
            band = _TYPE_BAND.get(row.get("type") or "", 6.0)
            by_band.setdefault(band, []).append((ident, row, degree, detail))
        canvas_nodes: list[CanvasNode] = []
        for band in sorted(by_band):
            row_items = sorted(
                by_band[band],
                key=lambda item: (-item[2], item[0][1]),
            )
            for index, (ident, row, degree, detail) in enumerate(row_items):
                node_id = f"{ident[0]}:{ident[1]}"
                jitter = int(hashlib.sha1(node_id.encode()).hexdigest()[:8], 16)
                x = float(index) + (jitter % 100) / 500.0
                y = band + ((jitter >> 8) % 100) / 500.0
                canvas_nodes.append(CanvasNode(
                    id=node_id,
                    type=row.get("type") or "",
                    label=(row.get("label") or row.get("key") or "")[:80],
                    detail=detail[:500],
                    confidence=int(row.get("confidence") or 0),
                    ts=str(row.get("ts") or row.get("date") or ""),
                    degree=degree,
                    x=x,
                    y=y,
                ))
        edge_out: list[tuple[str, str, str]] = []
        ids = {n.id for n in canvas_nodes}
        for e in view.edges:
            a = f"{e['from_type']}:{e['from_key']}"
            b = f"{e['to_type']}:{e['to_key']}"
            if a in ids and b in ids:
                edge_out.append((a, b, e.get("edge_type") or ""))
        return CanvasView(
            nodes=tuple(canvas_nodes),
            edges=tuple(edge_out),
            truncated=truncated,
            total_nodes=total,
        )

    def add_node(self, typ: str, key: str, label: str = "", confidence: int = 7,
                 source: str = "observed", extra: "dict | None" = None) -> dict:
        if typ not in GRAPH_NODE_TYPES:
            raise ValueError(f"unknown graph node type {typ!r}")
        row = {
            "ts": utc_now(),
            "type": typ,
            "key": key,
            "label": (label or key).strip(),
            "confidence": max(0, min(10, int(confidence))),
            "source": source,
        }
        if extra:
            row.update(extra)
        memory.append_jsonl(self.nodes_path(), row)
        return row

    def add_edge(self, from_type: str, from_key: str, to_type: str, to_key: str,
                 edge_type: str, note: str = "",
                 live_nodes: "dict | None" = None) -> dict:
        if from_type not in GRAPH_NODE_TYPES or to_type not in GRAPH_NODE_TYPES:
            raise ValueError("unknown node type")
        if edge_type not in GRAPH_EDGE_TYPES:
            raise ValueError(f"unknown edge type {edge_type!r}")
        nodes = live_nodes if live_nodes is not None else self.nodes()
        if _node_id(from_type, from_key) not in nodes:
            if from_type == "concept":
                self.add_node("concept", from_key, label=from_key)
                nodes = dict(nodes)
                nodes[_node_id(from_type, from_key)] = {"type": from_type, "key": from_key}
            else:
                raise ValueError(f"unknown node {from_type}:{from_key}")
        if _node_id(to_type, to_key) not in nodes:
            if to_type == "concept":
                self.add_node("concept", to_key, label=to_key)
                nodes = dict(nodes)
                nodes[_node_id(to_type, to_key)] = {"type": to_type, "key": to_key}
            else:
                raise ValueError(f"unknown node {to_type}:{to_key}")
        row = {
            "ts": utc_now(),
            "from_type": from_type,
            "from_key": from_key,
            "to_type": to_type,
            "to_key": to_key,
            "edge_type": edge_type,
            "note": (note or "").strip(),
        }
        memory.append_jsonl(self.edges_path(), row)
        return row

    def nodes(self) -> dict:
        """Latest node per (type, key), hiding effective confidence <= 0."""
        by_id: dict = {}
        for row in memory.read_jsonl(self.nodes_path()):
            typ, key = row.get("type") or "", row.get("key") or ""
            if typ and key:
                by_id[_node_id(typ, key)] = row
        return {k: r for k, r in by_id.items() if _graph_node_live(r)}

    def edges(self) -> list:
        """Edges whose endpoints still exist after decay filtering. Latest-wins."""
        return self._live_edges(self.nodes())

    def _live_edges(self, live: dict) -> list:
        last: dict[tuple, dict] = {}
        for row in memory.read_jsonl(self.edges_path()):
            a = _node_id(row.get("from_type") or "", row.get("from_key") or "")
            b = _node_id(row.get("to_type") or "", row.get("to_key") or "")
            et = row.get("edge_type") or ""
            if a not in live or b not in live or not et:
                continue
            last[(a, b, et)] = row
        return list(last.values())

    def on_learning(self, row: dict, cfg: "dict | None") -> None:
        if not self.enabled(cfg):
            return
        self.ensure(cfg)
        live = dict(self.load_view().nodes)
        self.add_node(
            "learning", row["key"], label=row.get("insight") or row["key"],
            confidence=int(row.get("confidence") or 7),
            source=row.get("source") or "observed",
            extra={
                "ts": row.get("ts") or utc_now(),
                "learning_type": row.get("type"),
            },
        )
        live[_node_id("learning", row["key"])] = row
        sess = _session_key()
        if sess:
            if _node_id("session", sess) not in live:
                self.add_node("session", sess, label=sess)
                live[_node_id("session", sess)] = {"type": "session", "key": sess}
            self.add_edge(
                "learning", row["key"], "session", sess, "in_session", note="auto",
                live_nodes=live,
            )
        for path in _paths_in_text(row.get("insight") or ""):
            if _node_id("file", path) not in live:
                self.add_node("file", path, label=path)
                live[_node_id("file", path)] = {"type": "file", "key": path}
            self.add_edge(
                "learning", row["key"], "file", path, "references", note="auto",
                live_nodes=live,
            )
        _BACKFILL_FP.pop(str(memory.project_dir(self.slug)), None)

    def on_decision(self, row: dict, cfg: "dict | None") -> None:
        if not self.enabled(cfg):
            return
        self.ensure(cfg)
        live = dict(self.load_view().nodes)
        self.add_node(
            "decision", row["id"], label=row.get("decision") or row["id"],
            extra={"ts": row.get("date") or utc_now()},
        )
        live[_node_id("decision", row["id"])] = row
        sess = _session_key()
        if sess:
            if _node_id("session", sess) not in live:
                self.add_node("session", sess, label=sess)
                live[_node_id("session", sess)] = {"type": "session", "key": sess}
            self.add_edge(
                "decision", row["id"], "session", sess, "in_session", note="auto",
                live_nodes=live,
            )
        hay = (row.get("decision") or "") + " " + (row.get("rationale") or "")
        for path in _paths_in_text(hay):
            if _node_id("file", path) not in live:
                self.add_node("file", path, label=path)
                live[_node_id("file", path)] = {"type": "file", "key": path}
            self.add_edge(
                "decision", row["id"], "file", path, "references", note="auto",
                live_nodes=live,
            )
        if row.get("supersedes"):
            if _node_id("decision", row["supersedes"]) in live:
                self.add_edge(
                    "decision", row["id"], "decision", row["supersedes"],
                    "supersedes", note="auto",
                    live_nodes=live,
                )
        _BACKFILL_FP.pop(str(memory.project_dir(self.slug)), None)

    def record_skill_use(self, skill_name: str, session: "Path | None",
                         cfg: "dict | None") -> None:
        if not self.enabled(cfg):
            return
        self.ensure(cfg)
        live = dict(self.load_view().nodes)
        if _node_id("skill", skill_name) not in live:
            self.add_node("skill", skill_name, label=skill_name)
            live[_node_id("skill", skill_name)] = {"type": "skill", "key": skill_name}
        path = session or memory._ACTIVE_SESSION
        if path is None:
            return
        sess = path.stem
        if _node_id("session", sess) not in live:
            self.add_node("session", sess, label=sess, extra={"path": str(path)})
            live[_node_id("session", sess)] = {"type": "session", "key": sess}
        self.add_edge(
            "session", sess, "skill", skill_name, "uses_skill", note="auto",
            live_nodes=live,
        )
        _BACKFILL_FP.pop(str(memory.project_dir(self.slug)), None)

    def neighbor_lines(self, ident: tuple, view: "GraphView | None" = None) -> list:
        if view is None:
            view = self.load_view()
        lines = []
        for dest, edge, direction in view.adjacency.get(ident, []):
            if dest not in view.nodes:
                continue
            lines.append(self._format_edge_line({
                "node": view.nodes[dest],
                "edge": edge,
                "direction": direction,
            }))
        return lines

    def search(self, query: str, learning_limit: int = 10,
               decision_limit: int = 10) -> str:
        view = self.load_view()
        terms = memory._query_terms(query)
        learnings = memory.get_learnings(query=query, limit=learning_limit, slug=self.slug)
        decisions = memory.get_decisions(limit=50, slug=self.slug)
        if terms:
            decisions = [
                d for d in decisions
                if any(t in (d.get("decision") or "").lower() for t in terms)
            ]
        decisions = decisions[:decision_limit]
        out = []
        if learnings:
            lines = []
            for r in learnings:
                lines.append(memory.format_learning_line(r))
                lines.extend(self.neighbor_lines(_node_id("learning", r["key"]), view))
            out.append("Learnings:\n" + "\n".join(lines))
        if decisions:
            lines = []
            for d in decisions:
                lines.append(memory.format_decision_line(d))
                lines.extend(self.neighbor_lines(_node_id("decision", d["id"]), view))
            out.append("Decisions:\n" + "\n".join(lines))
        if not out and terms:
            extra = []
            for ident, row in view.nodes.items():
                hay = (row.get("key", "") + " " + row.get("label", "")).lower()
                if any(t in hay for t in terms):
                    extra.append(self._format_node_line(row))
                    extra.extend(self.neighbor_lines(ident, view))
            if extra:
                out.append("Graph:\n" + "\n".join(extra))
        return "\n\n".join(out) or "(no memory matches)"

    def stats(self) -> str:
        """Adjacency-list stats: counts, orphans, contradiction clusters."""
        view = self.load_view()
        live = view.nodes
        edge_rows = view.edges
        by_type: dict[str, int] = {}
        for row in live.values():
            by_type[row["type"]] = by_type.get(row["type"], 0) + 1
        linked: set[tuple] = set()
        contradicts = []
        for e in edge_rows:
            a = _node_id(e["from_type"], e["from_key"])
            b = _node_id(e["to_type"], e["to_key"])
            linked.add(a)
            linked.add(b)
            if e.get("edge_type") == "contradicts":
                contradicts.append(e)
        orphans = [n for ident, n in live.items() if ident not in linked]
        lines = [
            f"nodes: {len(live)}  edges: {len(edge_rows)}",
            "by type: " + (", ".join(f"{t}={c}" for t, c in sorted(by_type.items())) or "(none)"),
            f"orphans: {len(orphans)}",
            f"contradicts: {len(contradicts)}",
        ]
        if orphans:
            lines.append("orphan nodes:")
            for n in orphans[:20]:
                lines.append(f"  ({n['type']}) {n['key']}")
        if contradicts:
            lines.append("contradiction pairs:")
            for e in contradicts[:20]:
                lines.append(
                    f"  {e['from_type']}:{e['from_key']} ⇄ {e['to_type']}:{e['to_key']}"
                    + (f" — {e['note']}" if e.get("note") else "")
                )
        adj_lines = []
        for ident, row in sorted(live.items(), key=lambda kv: (kv[0][0], kv[0][1])):
            nlines = self.neighbor_lines(ident, view)
            if not nlines:
                continue
            adj_lines.append(f"{row['type']}:{row['key']}")
            adj_lines.extend(nlines)
        if adj_lines:
            lines.append("adjacency:")
            lines.extend(adj_lines[:80])
        return "\n".join(lines)

    def contradiction_text(self) -> str:
        """Text dump of contradicts edges for /memory reconcile."""
        view = self.load_view()
        live = view.nodes
        pairs = [e for e in view.edges if e.get("edge_type") == "contradicts"]
        if not pairs:
            return "(no contradicts edges)"
        lines = []
        for e in pairs:
            a = live.get(_node_id(e["from_type"], e["from_key"]))
            b = live.get(_node_id(e["to_type"], e["to_key"]))
            lines.append(
                f"- {e['from_type']}:{e['from_key']}: {(a or {}).get('label', '')}\n"
                f"  vs {e['to_type']}:{e['to_key']}: {(b or {}).get('label', '')}"
                + (f"\n  note: {e['note']}" if e.get("note") else "")
            )
        return "\n".join(lines)

    @staticmethod
    def _source_fingerprint(root: Path) -> tuple:
        def _stat(path: Path) -> tuple:
            try:
                st = path.stat()
                return (st.st_size, st.st_mtime_ns)
            except OSError:
                return (0, 0)

        learn = root / "learnings.jsonl"
        dec = root / "decisions.jsonl"
        sess_dir = root / "sessions"
        session_count = 0
        sess_mtime = 0
        if sess_dir.is_dir():
            session_count = len(list(sess_dir.glob("*.jsonl")))
            try:
                sess_mtime = sess_dir.stat().st_mtime_ns
            except OSError:
                sess_mtime = 0
        return (_stat(learn), _stat(dec), sess_mtime, session_count)

    def _backfill(self) -> None:
        existing = self.nodes()
        wrote = False
        for r in memory.get_learnings(limit=10000, slug=self.slug):
            ident = _node_id("learning", r["key"])
            if ident not in existing:
                self.add_node(
                    "learning", r["key"], label=r.get("insight") or r["key"],
                    confidence=int(r.get("confidence") or 7),
                    source=r.get("source") or "observed",
                    extra={
                        "ts": r.get("ts") or utc_now(),
                        "learning_type": r.get("type"),
                    },
                )
                wrote = True
        for d in memory.get_decisions(limit=10000, slug=self.slug):
            ident = _node_id("decision", d["id"])
            if ident not in existing:
                self.add_node(
                    "decision", d["id"], label=d.get("decision") or d["id"],
                    extra={"ts": d.get("date") or utc_now()},
                )
                wrote = True
        for path in memory.all_sessions(slug=self.slug):
            ident = _node_id("session", path.stem)
            if ident not in existing:
                self.add_node(
                    "session", path.stem, label=memory.session_preview(path),
                    extra={"path": str(path)},
                )
                wrote = True
        if not wrote and not self.nodes_path().exists():
            path = self.nodes_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()

    def _adjacency(self, edge_rows: list) -> dict:
        adj: dict = {}
        for e in edge_rows:
            a = _node_id(e["from_type"], e["from_key"])
            b = _node_id(e["to_type"], e["to_key"])
            adj.setdefault(a, []).append((b, e, "out"))
            adj.setdefault(b, []).append((a, e, "in"))
        return adj

    def _format_node_line(self, row: dict) -> str:
        typ = row.get("type")
        if typ == "learning":
            subtype = row.get("learning_type") or "pattern"
            return (
                f"- ({subtype}, {row.get('confidence', '?')}/10) "
                f"{row.get('label') or row.get('insight', '')}"
            )
        if typ == "decision":
            return f"- [{row.get('key')}] {row.get('label')}"
        return f"- ({typ}) {row.get('key')}: {row.get('label')}"

    def _format_edge_line(self, hit: dict) -> str:
        e = hit["edge"]
        arrow = "←" if hit["direction"] == "in" else "→"
        other = hit["node"]
        return f"  {arrow} {e.get('edge_type')} {other.get('type')}:{other.get('key')}"


def graph_nodes_file(slug: "str | None" = None) -> Path:
    return KnowledgeGraph(slug).nodes_path()


def graph_edges_file(slug: "str | None" = None) -> Path:
    return KnowledgeGraph(slug).edges_path()


def add_graph_node(typ: str, key: str, label: str = "", confidence: int = 7,
                   source: str = "observed", extra: "dict | None" = None,
                   slug: "str | None" = None) -> dict:
    """Append a typed node. Latest row per (type, key) wins at read time."""
    return KnowledgeGraph(slug).add_node(
        typ, key, label=label, confidence=confidence, source=source, extra=extra,
    )


def add_graph_edge(from_type: str, from_key: str, to_type: str, to_key: str,
                   edge_type: str, note: str = "",
                   slug: "str | None" = None) -> dict:
    """Append an edge between existing nodes. Raises ValueError if invalid."""
    return KnowledgeGraph(slug).add_edge(
        from_type, from_key, to_type, to_key, edge_type, note=note,
    )


def get_graph_nodes(slug: "str | None" = None) -> dict:
    """Latest node per (type, key), hiding effective confidence <= 0."""
    return KnowledgeGraph(slug).nodes()


def get_graph_edges(slug: "str | None" = None) -> list:
    """Edges whose endpoints still exist after decay filtering. Latest-wins."""
    return KnowledgeGraph(slug).edges()


def record_workflow_run(
    cfg: dict, *, run_key: str, goal: str = "", commands: "list[str] | None" = None,
    slug: "str | None" = None,
) -> None:
    """Record one run, its goal, and hashed check-command concepts.

    No-op when ``use_graph`` is off. Calling twice with the same ``run_key``
    updates that node (latest row wins) instead of creating a second run.
    """
    kg = KnowledgeGraph(slug)
    if not kg.enabled(cfg):
        return
    kg.ensure(cfg)
    label = (goal or run_key)[:200]
    kg.add_node("run", run_key, label=label, source="observed")
    live = kg.nodes()
    if goal:
        gkey = hashlib.sha1(goal.encode()).hexdigest()[:12]
        kg.add_node("goal", gkey, label=goal[:200], source="observed")
        live = kg.nodes()
        kg.add_edge(
            "run", run_key, "goal", gkey, "leads_to", note="run",
            live_nodes=live,
        )
        live = kg.nodes()
    for cmd in commands or []:
        text = (cmd or "").strip()
        if not text:
            continue
        ckey = hashlib.sha1(text.encode()).hexdigest()[:16]
        if _node_id("concept", ckey) not in live:
            kg.add_node("concept", ckey, label=text[:160], source="observed")
            live = kg.nodes()
        kg.add_edge(
            "run", run_key, "concept", ckey, "related_to", note="check",
            live_nodes=live,
        )


def ensure_graph(cfg: dict, slug: "str | None" = None) -> None:
    """Backfill nodes from existing memory. No-op when use_graph is off."""
    KnowledgeGraph(slug).ensure(cfg)


def record_skill_use(skill_name: str, session: "Path | None" = None,
                     slug: "str | None" = None, cfg: "dict | None" = None) -> None:
    """Record a uses_skill edge from the current (or given) session."""
    KnowledgeGraph(slug).record_skill_use(skill_name, session, cfg)


def graph_stats(slug: "str | None" = None) -> str:
    """Adjacency-list stats: counts, orphans, contradiction clusters."""
    return KnowledgeGraph(slug).stats()


@dataclass(frozen=True)
class ReconcilePlan:
    """What ``/memory reconcile`` should do. At most one field is set."""

    notice: str = ""
    error: str = ""
    prompt: str = ""


def prepare_reconcile(cfg: dict, slug: "str | None" = None) -> ReconcilePlan:
    """Graph-off, empty cluster, missing skill, or the reconcile prompt."""
    if not cfg_bool(cfg, "use_graph"):
        return ReconcilePlan(notice=MSG_GRAPH_OFF)
    KnowledgeGraph(slug).ensure(cfg)
    cluster = contradiction_clusters(slug)
    if cluster.startswith("(no "):
        return ReconcilePlan(notice=cluster)
    from . import skills
    try:
        prompt = skills.load_skill("_reconcile") + "\n\n" + cluster
    except FileNotFoundError as exc:
        return ReconcilePlan(error=str(exc))
    return ReconcilePlan(prompt=prompt)


def contradiction_clusters(slug: "str | None" = None) -> str:
    """Text dump of contradicts edges for /memory reconcile."""
    return KnowledgeGraph(slug).contradiction_text()


def inspect_report(cfg: dict, slug: "str | None" = None) -> str:
    """``/memory graph`` body: stats plus contradiction clusters, or how to enable."""
    if not cfg_bool(cfg, "use_graph"):
        return MSG_GRAPH_OFF
    KnowledgeGraph(slug).ensure(cfg)
    return graph_stats(slug) + "\n" + contradiction_clusters(slug)


def format_canvas_text(cfg: dict, query: str = "", slug: "str | None" = None) -> str:
    """Text fallback when the full-screen canvas is unavailable."""
    if not cfg_bool(cfg, "use_graph"):
        return MSG_GRAPH_OFF
    kg = KnowledgeGraph(slug)
    kg.ensure(cfg)
    view = kg.canvas_view(query=query)
    lines = [
        f"canvas · {len(view.nodes)} nodes shown"
        + (f" (truncated from {view.total_nodes})" if view.truncated else ""),
        "",
    ]
    for node in sorted(view.nodes, key=lambda n: (n.y, n.x))[:60]:
        lines.append(
            f"  ({node.type}) {node.label} · deg {node.degree} · ({node.x:.1f},{node.y:.1f})",
        )
    if len(view.nodes) > 60:
        lines.append(f"  … {len(view.nodes) - 60} more (install prompt_toolkit for pan/zoom)")
    return "\n".join(lines)


def try_add_graph_edge(from_type: str, from_key: str, to_type: str, to_key: str,
                       edge_type: str, note: str,
                       slug: "str | None" = None) -> str:
    """Tool-facing wrapper: returns a saved line or ERROR: …"""
    note = (note or "").strip()
    if not note:
        return "ERROR: note is required"
    try:
        row = add_graph_edge(
            from_type, from_key, to_type, to_key, edge_type, note=note, slug=slug,
        )
    except ValueError as e:
        return f"ERROR: {e}"
    return (
        f"Saved edge {row['edge_type']} "
        f"{row['from_type']}:{row['from_key']} → {row['to_type']}:{row['to_key']}"
    )



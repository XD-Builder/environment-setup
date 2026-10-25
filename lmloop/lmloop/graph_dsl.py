"""Authored workflow graph DSL: parse, validate, and load (leaf)."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from .core.config import STATE_ROOT
from .until_parse import parse_until_args, split_check_flags

NODE_KINDS = frozenset({"skill", "until", "mine"})
EDGE_ON = frozenset({"pass", "fail", "blocked"})
RESERVED_GRAPH_NAMES = frozenset({"propose"})
GRAPH_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
GRAPHS_DIR = Path(__file__).parent / "graphs"
USER_GRAPHS_DIR = STATE_ROOT / "graphs"


class GraphError(Exception):
    """Invalid graph markdown or unknown graph name."""


@dataclass(frozen=True)
class NodeDef:
    name: str
    kind: str
    skill: str = ""
    task: str = ""
    goal: str = ""
    check_cmd: "str | None" = None
    checks: tuple = ()
    keeps: tuple = ()
    needs: tuple = ()
    control: "str | None" = None


@dataclass(frozen=True)
class EdgeDef:
    src: str
    dsts: tuple
    on: str = "pass"


@dataclass(frozen=True)
class GraphDef:
    name: str
    nodes: tuple
    edges: tuple

    @property
    def start(self) -> str:
        return self.nodes[0].name

    def node_map(self) -> dict:
        return {n.name: n for n in self.nodes}

    def node(self, name: str) -> NodeDef:
        found = self.node_map().get(name)
        if found is None:
            raise GraphError(f"unknown node {name!r}")
        return found

    def edge_for(self, src: str, on: str) -> "EdgeDef | None":
        for e in self.edges:
            if e.src == src and e.on == on:
                return e
        return None


def parse_graph(text: str, name: str) -> GraphDef:
    nodes: list[NodeDef] = []
    edges: list[EdgeDef] = []
    seen: set[str] = set()
    edge_keys: set[tuple] = set()
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            tokens = shlex.split(line)
        except ValueError as e:
            raise GraphError(str(e)) from e
        if not tokens:
            continue
        kind = tokens[0]
        if kind == "node":
            nodes.append(_parse_node(tokens[1:]))
            if nodes[-1].name in seen:
                raise GraphError(f"duplicate node name {nodes[-1].name!r}")
            seen.add(nodes[-1].name)
        elif kind == "edge":
            edge = _parse_edge(tokens[1:])
            key = (edge.src, edge.on)
            if key in edge_keys:
                raise GraphError(
                    f"duplicate edge from {edge.src!r} on {edge.on}"
                )
            edge_keys.add(key)
            edges.append(edge)
        else:
            raise GraphError(f"unknown line kind {kind!r}")
    if not nodes:
        raise GraphError("graph needs at least one node")
    names = {n.name for n in nodes}
    for e in edges:
        if e.src not in names:
            raise GraphError(f"edge source {e.src!r} is not a node")
        for dst in e.dsts:
            if dst not in names:
                raise GraphError(f"edge target {dst!r} is not a node")
    defn = GraphDef(name=name, nodes=tuple(nodes), edges=tuple(edges))
    _validate_graph(defn)
    return defn


def _split_needs(words: list) -> "tuple[tuple, list]":
    if "needs" not in words:
        return (), words
    idx = words.index("needs")
    needs = tuple(words[idx + 1:])
    if not needs:
        raise GraphError("node needs at least one predecessor name")
    return needs, words[:idx]


def _parse_node(tokens: list) -> NodeDef:
    if len(tokens) < 2:
        raise GraphError("node needs a name and a kind")
    name, kind = tokens[0], tokens[1]
    rest = tokens[2:]
    needs, rest = _split_needs(rest)
    if kind not in NODE_KINDS:
        raise GraphError(f"unknown node kind {kind!r}")
    if kind == "mine":
        if needs:
            raise GraphError(f"mine node {name!r} cannot have needs")
        return NodeDef(name=name, kind=kind)
    if kind == "until":
        parsed = parse_until_args(rest)
        if parsed.err or not parsed.goal:
            raise GraphError(f"until node {name!r} needs a goal")
        return NodeDef(
            name=name, kind=kind, goal=parsed.goal,
            check_cmd=parsed.check_cmd, checks=parsed.checks, keeps=parsed.keeps,
            needs=needs,
            control=parsed.control,
        )
    skill, task, checks, keeps = _parse_skill_rest(rest)
    return NodeDef(
        name=name, kind=kind, skill=skill, task=task,
        check_cmd=checks[0] if checks else None, checks=checks, keeps=keeps,
        needs=needs,
    )


def _parse_skill_rest(words: list) -> "tuple[str, str, tuple, tuple]":
    checks, keeps, _control, rest, err = split_check_flags(words)
    if err:
        raise GraphError("skill node --check/--keep needs a command")
    if not rest:
        raise GraphError("skill node needs a skill name")
    return rest[0], " ".join(rest[1:]).strip(), checks, keeps


def _parse_edge(tokens: list) -> EdgeDef:
    if len(tokens) < 3 or tokens[1] != "->":
        raise GraphError("edge needs: <from> -> <to> [on pass|fail|blocked]")
    src = tokens[0]
    rest = tokens[2:]
    on = "pass"
    if len(rest) >= 2 and rest[-2] == "on":
        on = rest[-1]
        if on not in EDGE_ON:
            raise GraphError(f"unknown edge on {on!r}")
        dst_tokens = rest[:-2]
    else:
        dst_tokens = rest
    if not dst_tokens:
        raise GraphError("edge needs at least one target")
    if on != "pass" and len(dst_tokens) > 1:
        raise GraphError(
            f"multi-target edge from {src!r} on {on!r} is not allowed"
        )
    return EdgeDef(src=src, dsts=tuple(dst_tokens), on=on)


def _validate_graph(defn: GraphDef) -> None:
    nmap = defn.node_map()
    names = set(nmap)
    order = [n.name for n in defn.nodes]

    for node in defn.nodes:
        for need in node.needs:
            if need not in names:
                raise GraphError(
                    f"unknown need {need!r} on node {node.name!r}"
                )
            if need == node.name:
                raise GraphError(
                    f"node {node.name!r} cannot need itself"
                )
        if node.kind == "mine" and node.needs:
            raise GraphError(f"mine node {node.name!r} cannot have needs")

    for edge in defn.edges:
        if edge.on != "pass" and len(edge.dsts) > 1:
            raise GraphError(
                f"multi-target edge from {edge.src!r} on {edge.on!r} "
                "is not allowed"
            )
        if len(edge.dsts) > 1:
            for dst in edge.dsts:
                if nmap[dst].kind == "mine":
                    raise GraphError(
                        f"mine node {dst!r} cannot be a multi-target edge "
                        f"destination from {edge.src!r}"
                    )

    indeg = {n: 0 for n in order}
    adj: dict[str, list[str]] = {n: [] for n in order}
    for node in defn.nodes:
        for need in node.needs:
            adj[need].append(node.name)
            indeg[node.name] += 1
    queue = [n for n in order if indeg[n] == 0]
    seen = 0
    while queue:
        n = queue.pop(0)
        seen += 1
        for dst in adj.get(n, []):
            indeg[dst] -= 1
            if indeg[dst] == 0:
                queue.append(dst)
    if seen != len(order):
        raise GraphError("needs form a cycle")

    reachable = edge_reachable_from(defn, defn.start)
    for node in defn.nodes:
        for need in node.needs:
            if need not in reachable:
                raise GraphError(
                    f"need {need!r} on node {node.name!r} is unreachable "
                    "from the start"
                )


def edge_reachable_from(defn: GraphDef, start: str) -> set[str]:
    seen = {start}
    queue = [start]
    while queue:
        n = queue.pop(0)
        for on in EDGE_ON:
            edge = defn.edge_for(n, on)
            if not edge:
                continue
            for dst in edge.dsts:
                if dst not in seen:
                    seen.add(dst)
                    queue.append(dst)
    return seen


def reachable_from_frontier(defn: GraphDef, frontier: set[str]) -> set[str]:
    seen = set(frontier)
    queue = list(frontier)
    while queue:
        n = queue.pop(0)
        for on in EDGE_ON:
            edge = defn.edge_for(n, on)
            if not edge:
                continue
            for dst in edge.dsts:
                if dst not in seen:
                    seen.add(dst)
                    queue.append(dst)
    return seen


def graph_dirs() -> list[Path]:
    dirs = []
    if USER_GRAPHS_DIR.is_dir():
        dirs.append(USER_GRAPHS_DIR)
    dirs.append(GRAPHS_DIR)
    return dirs


def list_graphs() -> list[str]:
    names: dict[str, None] = {}
    for d in reversed(graph_dirs()):
        if not d.is_dir():
            continue
        for path in sorted(d.glob("*.md")):
            stem = path.stem
            if GRAPH_NAME_RE.match(stem):
                names[stem] = None
    return list(names)


def graph_path(name: str) -> "Path | None":
    for d in graph_dirs():
        path = d / f"{name}.md"
        if path.is_file():
            return path
    return None


def load_graph(name: str) -> GraphDef:
    if name in RESERVED_GRAPH_NAMES:
        raise GraphError(
            f"{name!r} is reserved — use `lmloop graph propose [name]` to draft a graph",
        )
    if not GRAPH_NAME_RE.match(name or ""):
        raise GraphError(f"invalid graph name {name!r}")
    path = graph_path(name)
    if path is None:
        known = ", ".join(list_graphs()) or "(none)"
        raise GraphError(f"unknown graph {name!r}. graphs: {known}")
    return parse_graph(path.read_text(), name)

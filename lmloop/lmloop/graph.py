"""Sparse authored workflow graphs: named loops with explicit edges.

A graph is a list of nodes (skill / until / mine) plus authored edges.
Each node runs isolated. This module must not be imported by loop, agent,
stream, or display.
"""

import difflib
import re
import shlex
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import knowledge_graph, memory, skills, snapshot, status as status_mod, tools
from .config import DEFAULTS, STATE_ROOT, cfg_bool, cfg_int, project_dir, utc_now
from .loop import (
    DONE_ROLES,
    EVAL_PROMPT,
    META_ROLE,
    PAUSE_ROLE,
    UntilRun,
    approved_note,
    boundary_approval,
    drop_denied,
    eval_max_rounds,
    isolated_act,
    last_assistant,
    parse_eval_status,
    parse_until_args,
    run_plan_commands,
    run_until,
    split_check_flags,
)

NODE_KINDS = frozenset({"skill", "until", "mine"})
EDGE_ON = frozenset({"pass", "fail", "blocked"})
JOIN_HANDOFF_CLIP = 1500
PROPOSE_MIN_TERMINAL_RUNS = 5
RESERVED_GRAPH_NAMES = frozenset({"propose"})
GRAPH_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
GRAPHS_DIR = Path(__file__).parent / "graphs"
USER_GRAPHS_DIR = STATE_ROOT / "graphs"


class GraphError(Exception):
    """Invalid graph markdown or unknown graph name."""


@dataclass(frozen=True)
class NodeDef:
    """One graph node: skill, until, or mine."""

    name: str
    kind: str
    skill: str = ""
    task: str = ""
    goal: str = ""
    check_cmd: "str | None" = None
    checks: tuple = ()
    keeps: tuple = ()
    needs: tuple = ()


@dataclass(frozen=True)
class EdgeDef:
    """Authored edge. ``on`` defaults to pass."""

    src: str
    dsts: tuple
    on: str = "pass"


@dataclass(frozen=True)
class GraphDef:
    """Parsed graph. Start node is the first ``node`` line."""

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
    """Parse line-based graph markdown. Headings and blanks are ignored."""
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
        )
    skill, task, checks, keeps = _parse_skill_rest(rest)
    return NodeDef(
        name=name, kind=kind, skill=skill, task=task,
        check_cmd=checks[0] if checks else None, checks=checks, keeps=keeps,
        needs=needs,
    )


def _parse_skill_rest(words: list) -> "tuple[str, str, tuple, tuple]":
    checks, keeps, rest, err = split_check_flags(words)
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
    """Fail closed on needs/edge rules before any model call."""
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

    # Needs-only cycle (retry cycles via edges stay legal).
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

    # Every need predecessor must be reachable from the start via edges.
    reachable = _edge_reachable_from(defn, defn.start)
    for node in defn.nodes:
        for need in node.needs:
            if need not in reachable:
                raise GraphError(
                    f"need {need!r} on node {node.name!r} is unreachable "
                    "from the start"
                )


def _edge_reachable_from(defn: GraphDef, start: str) -> set[str]:
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


def _reachable_from_frontier(defn: GraphDef, frontier: set[str]) -> set[str]:
    """Nodes that may still be scheduled from the current frontier."""
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


def graph_dirs() -> "list[Path]":
    """Search order: user graphs override packaged graphs."""
    dirs = []
    if USER_GRAPHS_DIR.is_dir():
        dirs.append(USER_GRAPHS_DIR)
    dirs.append(GRAPHS_DIR)
    return dirs


def list_graphs() -> "list[str]":
    """Graph names from packaged + ~/.lmloop/graphs/."""
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
    """Load and validate a packaged or user graph."""
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


def graphs_dir(slug: "str | None" = None) -> Path:
    d = project_dir(slug) / "graphs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def all_graph_runs(slug: "str | None" = None) -> "list[Path]":
    d = project_dir(slug) / "graphs"
    if not d.exists():
        return []
    return sorted(d.glob("*/*.jsonl"))


@dataclass
class GraphRun:
    """Append-only graph-run log. Current node is computed from events."""

    path: Path
    name: str
    events: list = field(default_factory=list)

    @classmethod
    def create(cls, name: str, slug: "str | None" = None) -> "GraphRun":
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        d = graphs_dir(slug) / name
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{ts}.jsonl"
        n = 1
        while path.exists():
            path = d / f"{ts}-{n}.jsonl"
            n += 1
        run = cls(path=path, name=name, events=[])
        run._write({"ts": utc_now(), "role": META_ROLE, "name": name})
        return run

    @classmethod
    def load(cls, path: Path) -> "GraphRun":
        events = memory.read_jsonl(path)
        name = ""
        if events and events[0].get("role") == META_ROLE:
            name = events[0].get("name") or ""
        if not name:
            name = path.parent.name
        return cls(path=path, name=name, events=events)

    def _write(self, row: dict) -> None:
        memory.append_jsonl(self.path, row)
        self.events.append(row)

    def append(self, role: str, status: str, node: str = "",
               handoff: str = "", session: str = "",
               until_run: str = "", verify=None, snapshot_ref: str = "") -> None:
        step = sum(1 for e in self.events if e.get("role") not in (META_ROLE,))
        row = {
            "ts": utc_now(),
            "step": step,
            "role": role,
            "status": status,
            "node": node,
            "handoff": handoff,
            "session": session,
            "until_run": until_run,
        }
        if verify:
            row["verify"] = verify
        if snapshot_ref:
            row["snapshot_ref"] = snapshot_ref
        self._write(row)

    def last_work(self) -> "dict | None":
        for ev in reversed(self.events):
            if ev.get("role") not in (META_ROLE, PAUSE_ROLE):
                return ev
        return None

    def last_pause(self) -> "dict | None":
        if self.events and self.events[-1].get("role") == PAUSE_ROLE:
            return self.events[-1]
        return None

    def is_paused(self) -> bool:
        if not self.events:
            return False
        return self.events[-1].get("role") == PAUSE_ROLE

    def is_done(self) -> bool:
        last = self.last_work()
        if last is None:
            return False
        if last.get("role") in DONE_ROLES:
            return True
        return last.get("role") == "gate" and last.get("status") == "no"

    def last_handoff(self) -> str:
        for ev in reversed(self.events):
            if ev.get("handoff"):
                return ev.get("handoff") or ""
        return ""

    def handoff_for_node(self, node_name: str) -> str:
        for ev in reversed(self.events):
            if ev.get("role") == "node" and ev.get("node") == node_name:
                return ev.get("handoff") or ""
        return ""

    def join_handoff(self, node: NodeDef) -> str:
        """Labeled predecessor summaries in ``needs`` order, clipped."""
        if not node.needs:
            return self.last_handoff()
        parts: list[str] = []
        for need in node.needs:
            summary = self.handoff_for_node(need)
            if len(summary) > JOIN_HANDOFF_CLIP:
                summary = summary[:JOIN_HANDOFF_CLIP] + "…"
            parts.append(f"[{need}]\n{summary}")
        return "\n\n".join(parts)

    def _replay_frontier(
        self, defn: GraphDef,
    ) -> "tuple[set[str], list[tuple[str, str]], dict[str, str]]":
        """Return (frontier, unrouted failures, latest node status)."""
        frontier: set[str] = {defn.start}
        unrouted: list[tuple[str, str]] = []
        latest: dict[str, str] = {}
        for ev in self.events:
            if ev.get("role") != "node":
                continue
            name = ev.get("node") or ""
            status = ev.get("status") or "pass"
            if not name:
                continue
            latest[name] = status
            frontier.discard(name)
            if status == "pass":
                edge = defn.edge_for(name, "pass")
                if edge:
                    frontier.update(edge.dsts)
            elif status in ("fail", "blocked"):
                edge = defn.edge_for(name, status)
                if edge:
                    frontier.add(edge.dsts[0])
                else:
                    unrouted.append((name, status))
        return frontier, unrouted, latest

    def _needs_satisfied(self, node: NodeDef, latest: dict[str, str]) -> bool:
        return all(latest.get(need) == "pass" for need in node.needs)

    def _unsatisfiable_waits(
        self, defn: GraphDef, frontier: set[str], latest: dict[str, str],
    ) -> list[tuple[str, str]]:
        reachable = _reachable_from_frontier(defn, frontier)
        bad: list[tuple[str, str]] = []
        for name in frontier:
            node = defn.node(name)
            for need in node.needs:
                if latest.get(need) == "pass":
                    continue
                if need in frontier or need in reachable:
                    continue
                bad.append((name, need))
        return bad

    def session_paths(self) -> "list[Path]":
        seen: list[Path] = []
        have: set[str] = set()

        def add(raw: str) -> None:
            if not raw or raw in have:
                return
            have.add(raw)
            seen.append(Path(raw))

        for ev in self.events:
            until_raw = ev.get("until_run") or ""
            if until_raw:
                until_path = Path(until_raw)
                if until_path.is_file():
                    for p in UntilRun.load(until_path).session_paths():
                        add(str(p))
                    continue
            add(ev.get("session") or "")
        return seen

    def resume_until(self, node_name: str) -> "str | None":
        pause = self.last_pause()
        if pause and pause.get("node") == node_name:
            return pause.get("until_run") or None
        return None

    def frontier_state(self, defn: GraphDef) -> "tuple[list[str], str]":
        """Runnable node names and a phase: run, done, blocked, paused, or stopped."""
        if self.is_paused():
            return [], "paused"
        if self.is_done():
            return [], "done"
        last = self.last_work()
        if last is None:
            return [defn.start], "run"
        if last.get("role") == "gate":
            if last.get("status") == "yes" and last.get("node"):
                return [last["node"]], "run"
            return [], "stopped"
        frontier, unrouted, latest = self._replay_frontier(defn)
        if unrouted:
            return [], "blocked"
        names = [
            name for name in (node.name for node in defn.nodes)
            if name in frontier and self._needs_satisfied(defn.node(name), latest)
        ]
        if names:
            return names, "run"
        if frontier:
            return [], "blocked"
        return [], "done"

    def runnable_names(self, defn: GraphDef) -> list[str]:
        names, _phase = self.frontier_state(defn)
        return names

    def next_step(self, defn: GraphDef) -> "tuple[str | None, str | None]":
        """Return (kind, node_name). kind is run, gate, or None if done."""
        last = self.last_work()
        if last is None:
            return "run", defn.start
        role, status = last.get("role"), last.get("status")
        if role == "gate":
            if status == "yes":
                node = last.get("node") or ""
                if node:
                    return "run", node
            else:
                return None, None
        elif role in DONE_ROLES:
            return None, None

        frontier, unrouted, latest = self._replay_frontier(defn)
        if not frontier and not unrouted:
            return None, None

        declaration = [n.name for n in defn.nodes]
        runnable = [
            n for n in declaration
            if n in frontier and self._needs_satisfied(defn.node(n), latest)
        ]
        if runnable:
            return "run", runnable[0]

        unsat = self._unsatisfiable_waits(defn, frontier, latest)
        if unrouted or unsat:
            gate_node = unrouted[0][0] if unrouted else ""
            return "gate", gate_node
        return "gate", ""


def latest_open_graph_run(slug: "str | None" = None) -> "GraphRun | None":
    """Most recent graph-run that is not done (paused or in progress)."""
    for path in reversed(all_graph_runs(slug=slug)):
        run = GraphRun.load(path)
        if not run.is_done():
            return run
    return None


def superseded_graph_hint(slug: "str | None" = None) -> "str | None":
    prior = latest_open_graph_run(slug)
    if prior is None:
        return None
    return (
        f"[graph · previous run {prior.path.name} still open; "
        "resume uses the latest run]"
    )


def _pause_interrupted(run: GraphRun, echo_status, node: str = "",
                       until_run: str = "") -> GraphRun:
    if not run.is_paused() and not run.is_done():
        run.append(PAUSE_ROLE, "paused", node=node, until_run=until_run)
        echo_status(status_mod.msg_graph_paused())
    return run


def _skill_prompt(node: NodeDef, handoff: str) -> str:
    body = skills.load_skill(node.skill, public_only=True)
    parts = [body]
    if node.task:
        parts.append("Task: " + node.task)
    if handoff:
        parts.append("Prior handoff:\n" + handoff)
    return "\n\n".join(parts)


def _run_skill_node(
    cfg: dict, model: str, node: NodeDef, handoff: str, *,
    confirm_gate, echo, echo_status, echo_error, echo_tool, echo_round,
    context_limit, context_reserve, workspace_root,
    ask_gate=None,
    clock_now=None,
) -> "tuple[str, str, str]":
    """Return (status, handoff, session_path). status pause means stop the graph.

    When the GatePolicy denied irreversible actions during the act, ask once
    and re-run the node with those actions pre-approved (one retry).
    """
    try:
        prompt = _skill_prompt(node, handoff)
    except FileNotFoundError as e:
        echo_error(str(e))
        return "blocked", str(e), ""
    act_kwargs = dict(
        confirm_gate=confirm_gate, echo=echo, echo_status=echo_status,
        echo_error=echo_error, echo_tool=echo_tool, echo_round=echo_round,
        context_limit=context_limit, context_reserve=context_reserve,
        workspace_root=workspace_root,
        log_label=f"/graph {node.name}",
        clock_now=clock_now,
    )
    result = isolated_act(cfg, model, prompt, **act_kwargs)
    if result is None:
        return "pause", "", ""
    approved, _denied = boundary_approval(
        confirm_gate, ask_gate, echo_status, label=f"graph {node.name}",
    )
    if approved:
        messages, _prior = result
        retry_prompt = approved_note(approved) + _skill_prompt(
            node, last_assistant(messages) or handoff,
        )
        result = isolated_act(cfg, model, retry_prompt, **act_kwargs)
        if result is None:
            return "pause", "", ""
    messages, session_log = result
    knowledge_graph.record_skill_use(node.skill, session=session_log, cfg=cfg)
    summary = last_assistant(messages)
    command_status = _skill_command_status(
        cfg, node, confirm_gate, workspace_root, echo_status,
    )
    if command_status is not None:
        return command_status, summary, str(session_log)
    eval_prompt = EVAL_PROMPT.format(
        goal=node.task or f"complete the {node.skill} playbook",
        handoff=summary or "(none)",
        check_output="(none)",
        shell_evidence="(none)",
    )
    ev = isolated_act(
        cfg, model, eval_prompt,
        confirm_gate=confirm_gate, echo=echo, echo_status=echo_status,
        echo_error=echo_error, echo_tool=echo_tool, echo_round=echo_round,
        context_limit=context_limit, context_reserve=context_reserve,
        workspace_root=workspace_root,
        log_label=f"/graph {node.name} eval",
        readonly=True,
        max_rounds=eval_max_rounds(cfg),
        clock_now=clock_now,
    )
    drop_denied(confirm_gate)
    if ev is None:
        return "pause", summary, str(session_log)
    ev_messages, _ev_session = ev
    status = parse_eval_status(last_assistant(ev_messages))
    return status, summary, str(session_log)


def _skill_command_status(cfg, node: NodeDef, confirm_gate, workspace_root,
                          echo_status) -> "str | None":
    """Run explicit skill --check/--keep commands.

    Returns pass, fail, or blocked. None means there is no check command, or
    only keeps passed and the eval checker must still judge the task.
    """
    checks = tuple(node.checks or (() if not node.check_cmd else (node.check_cmd,)))
    keeps = tuple(node.keeps or ())
    if not checks and not keeps:
        return None
    results = run_plan_commands(
        cfg, list(checks) + list(keeps), confirm_gate, workspace_root,
    )
    for row in results:
        echo_status(row["output"])
    if any(row["status"] == "blocked" for row in results):
        return "blocked"
    if any(row["status"] != "pass" for row in results):
        return "fail"
    if checks:
        return "pass"
    return None


def _run_until_node(
    cfg: dict, model: str, node: NodeDef, handoff: str, *,
    resume_until: "str | None",
    confirm_gate, echo, echo_status, echo_error, echo_tool, echo_round,
    context_limit, context_reserve, workspace_root, ask_gate,
    clock_now=None,
) -> "tuple[str, str, str, str, list]":
    """Return (status, handoff, session, until_path, verify)."""
    if resume_until:
        urun = UntilRun.load(Path(resume_until))
    else:
        urun = UntilRun.create(
            node.goal, checks=node.checks, keeps=node.keeps,
        )
    result = run_until(
        cfg, model, run=urun,
        confirm_gate=confirm_gate, echo=echo, echo_status=echo_status,
        echo_error=echo_error, echo_tool=echo_tool, echo_round=echo_round,
        context_limit=context_limit, context_reserve=context_reserve,
        workspace_root=workspace_root, ask_gate=ask_gate, mine=None,
        seed_handoff=handoff,
        clock_now=clock_now,
    )
    until_path = str(result.path)
    paths = result.session_paths()
    session = str(paths[-1]) if paths else ""
    summary = result.last_maker_handoff() or ""
    verify = [item.as_dict() for item in result.current_checks()]
    if result.is_paused():
        return "pause", summary, session, until_path, verify
    last = result.last_work()
    if last and last.get("role") == "gate" and last.get("status") == "no":
        return "blocked", summary, session, until_path, verify
    return "pass", summary, session, until_path, verify


def run_named_node(
    cfg: dict, model: str, defn: GraphDef, node_name: str, handoff: str, *,
    workspace_root: "Path | None" = None,
    confirm_gate=None,
) -> dict:
    """Run one graph node. A mine node does not write memory."""
    node = defn.node(node_name)
    if node.kind == "mine":
        return {
            "status": "pass",
            "summary": "mine stays on the orchestrator",
            "session": "",
        }
    quiet = lambda *_args, **_kwargs: None
    root = Path(workspace_root).resolve() if workspace_root else Path.cwd().resolve()
    clock_now = datetime.now(timezone.utc)
    if node.kind == "skill":
        status, summary, session = _run_skill_node(
            cfg, model, node, handoff,
            confirm_gate=confirm_gate, echo=quiet, echo_status=quiet,
            echo_error=quiet, echo_tool=quiet, echo_round=quiet,
            context_limit=0, context_reserve=DEFAULTS["context_reserve"],
            workspace_root=root, clock_now=clock_now,
        )
        return {"status": status, "summary": summary, "session": session}
    if node.kind == "until":
        status, summary, session, until_path, _verify = _run_until_node(
            cfg, model, node, handoff, resume_until=None,
            confirm_gate=confirm_gate, echo=quiet, echo_status=quiet,
            echo_error=quiet, echo_tool=quiet, echo_round=quiet,
            context_limit=0, context_reserve=DEFAULTS["context_reserve"],
            workspace_root=root, ask_gate=None, clock_now=clock_now,
        )
        return {
            "status": status, "summary": summary, "session": session,
            "until_run": until_path,
        }
    raise RuntimeError(f"unknown graph node kind {node.kind!r}")


def run_graph(
    cfg: dict, model: str, *,
    run: GraphRun,
    defn: GraphDef,
    confirm_gate=None,
    echo=print,
    echo_status=None,
    echo_tool=None,
    echo_error=None,
    echo_round=None,
    context_limit: int = 0,
    context_reserve: int = DEFAULTS["context_reserve"],
    workspace_root: "Path | None" = None,
    ask_gate=None,
    mine=None,
) -> GraphRun:
    """Advance ``run`` until pass, gate-no, pause, or interrupt. Mutates run."""
    from . import usage

    usage.record("graph.run", name=run.name)
    if echo_status is None:
        echo_status = echo
    root = Path(workspace_root).resolve() if workspace_root else Path.cwd().resolve()
    do_mine = cfg_bool(cfg, "graph_mine") and mine is not None
    max_steps = max(1, cfg_int(cfg, "graph_max_steps"))
    steps_this_call = 0
    current_node = ""
    current_until = ""
    clock_now = datetime.now(timezone.utc)
    # One policy for the whole graph run; nested run_until reuses it as-is.
    confirm_gate = tools.autonomous_gate(cfg, confirm_gate, echo_status)

    try:
        while True:
            if run.is_done():
                return run
            kind, node_name = run.next_step(defn)
            if kind is None:
                if do_mine:
                    echo_status(status_mod.msg_graph_mining())
                    paths = [p for p in run.session_paths() if p.exists()]
                    mine(paths)
                    run.append("mine", "next")
                run.append("done", "pass")
                echo_status(status_mod.msg_graph_done())
                return run
            if kind == "gate":
                echo_status(status_mod.msg_graph_blocked())
                ok = False
                if ask_gate is not None:
                    ok = bool(ask_gate(
                        "No edge for this outcome. Continue from this node? [y/N] "
                    ))
                run.append("gate", "yes" if ok else "no", node=node_name or "")
                continue
            node = defn.node(node_name or "")
            current_node = node.name
            if node.kind == "mine":
                echo_status(status_mod.msg_graph_mining())
                paths = [p for p in run.session_paths() if p.exists()]
                if mine is not None:
                    mine(paths)
                run.append("mine", "next", node=node.name)
                echo_status(status_mod.msg_graph_done())
                return run
            if steps_this_call >= max_steps:
                run.append(PAUSE_ROLE, "paused", node=node.name)
                echo_status(status_mod.msg_graph_max_steps())
                return run
            steps_this_call += 1
            snap = snapshot.take_snapshot(
                cfg, root,
                run_label=f"{run.name}-{run.path.stem}",
                step=steps_this_call,
            )
            echo_status(status_mod.msg_graph_step(
                node.name, steps_this_call, max_steps,
            ))
            handoff = run.join_handoff(node)
            snap_ref = snap.ref
            if node.kind == "skill":
                status, summary, session = _run_skill_node(
                    cfg, model, node, handoff,
                    confirm_gate=confirm_gate, echo=echo,
                    echo_status=echo_status, echo_error=echo_error,
                    echo_tool=echo_tool, echo_round=echo_round,
                    context_limit=context_limit,
                    context_reserve=context_reserve, workspace_root=root,
                    ask_gate=ask_gate,
                    clock_now=clock_now,
                )
                if status == "pause":
                    return _pause_interrupted(run, echo_status, node=node.name)
                run.append(
                    "node", status, node=node.name,
                    handoff=summary, session=session,
                    snapshot_ref=snap_ref,
                )
                continue
            if node.kind == "until":
                resume = run.resume_until(node.name)
                current_until = resume or ""
                status, summary, session, until_path, verify = _run_until_node(
                    cfg, model, node, handoff, resume_until=resume,
                    confirm_gate=confirm_gate, echo=echo,
                    echo_status=echo_status, echo_error=echo_error,
                    echo_tool=echo_tool, echo_round=echo_round,
                    context_limit=context_limit,
                    context_reserve=context_reserve, workspace_root=root,
                    ask_gate=ask_gate,
                    clock_now=clock_now,
                )
                current_until = until_path
                if status == "pause":
                    run.append(
                        PAUSE_ROLE, "paused", node=node.name,
                        until_run=until_path,
                    )
                    echo_status(status_mod.msg_graph_paused())
                    return run
                run.append(
                    "node", status, node=node.name,
                    handoff=summary, session=session, until_run=until_path,
                    verify=verify,
                    snapshot_ref=snap_ref,
                )
                continue
            raise RuntimeError(f"unknown graph node kind {node.kind!r}")
    except KeyboardInterrupt:
        return _pause_interrupted(
            run, echo_status, node=current_node, until_run=current_until,
        )
    finally:
        from .knowledge_graph import record_workflow_run
        cmds = []
        for node in defn.nodes:
            cmds.extend(list(node.checks or ()))
            cmds.extend(list(node.keeps or ()))
        record_workflow_run(
            cfg, run_key=f"{run.name}:{run.path.stem}",
            goal=run.name, commands=cmds,
        )


def is_packaged_graph(name: str) -> bool:
    """True when only the packaged copy exists (never overwrite on propose)."""
    user = USER_GRAPHS_DIR / f"{name}.md"
    if user.is_file():
        return False
    return (GRAPHS_DIR / f"{name}.md").is_file()


def _command_argv0(cmd: str) -> str:
    try:
        parts = shlex.split(cmd)
    except ValueError:
        return ""
    return parts[0] if parts else ""


def _probe_argv0(argv0: str, *, docker: bool) -> bool:
    if not argv0:
        return False
    if argv0.startswith(("./", "/")):
        return True
    if shutil.which(argv0):
        return True
    if docker:
        import subprocess
        try:
            proc = subprocess.run(
                ["sh", "-c", 'command -v "$1"', "_", argv0],
                capture_output=True, text=True, check=False,
            )
            return proc.returncode == 0 and bool(proc.stdout.strip())
        except OSError:
            return False
    return False


def annotate_unverified_commands(text: str, *, docker: bool) -> str:
    """Append ``# unverified:`` for lines whose argv0 is missing on the host."""
    out: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("node ") and " until " in stripped:
            m = re.search(r" until .*?(--check\s+\S+\s+)?(.+)$", stripped)
            if m and "--check" in stripped:
                chk = re.search(r"--check\s+(\S+(?:\s+\S+)*)", stripped)
                if chk:
                    cmd = chk.group(1).strip("`'\"")
                    if not _probe_argv0(_command_argv0(cmd), docker=docker):
                        line = line.rstrip() + f"  # unverified: {cmd}"
        out.append(line)
    return "\n".join(out)


def propose_graph_draft(
    cfg: dict, model: str, name: str, *,
    echo_status,
    docker: bool = False,
    slug: "str | None" = None,
) -> "str | None":
    """Model draft of a graph; returns markdown or None when refused / unparseable."""
    from . import workflow
    from .loop import eval_model_name

    if name in RESERVED_GRAPH_NAMES:
        raise GraphError(f"invalid target name {name!r}")
    if not GRAPH_NAME_RE.match(name or ""):
        raise GraphError(f"invalid graph name {name!r}")
    if is_packaged_graph(name):
        raise GraphError(f"refusing to overwrite packaged graph {name!r}")
    terminal = workflow.count_terminal_runs(slug)
    if terminal < PROPOSE_MIN_TERMINAL_RUNS:
        raise GraphError(
            f"need at least {PROPOSE_MIN_TERMINAL_RUNS} finished runs "
            f"(have {terminal}) — run more until/graph workflows first",
        )
    stats = workflow.collect_flow_stats(slug)
    summary = workflow.compact_flow_summary(stats, cfg)
    existing = ""
    path = graph_path(name)
    if path is not None:
        existing = path.read_text()
    try:
        skill = skills.load_skill("_graph_author")
    except FileNotFoundError as e:
        raise GraphError(str(e)) from e
    prompt = (
        f"{skill}\n\n"
        f"Target graph name: {name}\n\n"
        f"Run statistics:\n{summary}\n\n"
    )
    if existing.strip():
        prompt += f"Existing graph:\n{existing}\n\n"
    prompt += "Output the full revised graph markdown now."
    result = isolated_act(
        cfg, model, prompt,
        confirm_gate=None,
        echo=echo_status,
        echo_status=echo_status,
        workspace_root=Path.cwd().resolve(),
        log_label="/graph propose",
        no_tools=True,
        max_rounds=1,
        model_override=eval_model_name(cfg, model),
    )
    if result is None:
        return None
    messages, _session = result
    draft = last_assistant(messages).strip()
    if not draft:
        return None
    try:
        parse_graph(draft, name)
    except GraphError:
        echo_status("draft did not parse — nothing saved")
        return None
    return annotate_unverified_commands(draft, docker=docker)


def save_proposed_graph(name: str, text: str) -> Path:
    if is_packaged_graph(name):
        raise GraphError(f"refusing to overwrite packaged graph {name!r}")
    USER_GRAPHS_DIR.mkdir(parents=True, exist_ok=True)
    path = USER_GRAPHS_DIR / f"{name}.md"
    path.write_text(text if text.endswith("\n") else text + "\n")
    return path


def diff_proposed_graph(name: str, draft: str) -> str:
    existing = ""
    path = graph_path(name)
    if path is not None:
        existing = path.read_text()
    if existing == draft:
        return "(no changes)"
    lines = difflib.unified_diff(
        existing.splitlines(keepends=True),
        draft.splitlines(keepends=True),
        fromfile=f"{name}.md (current)",
        tofile=f"{name}.md (proposed)",
    )
    return "".join(lines) or "(no changes)"

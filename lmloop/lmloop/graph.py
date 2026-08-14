"""Sparse authored workflow graphs: named loops with explicit edges.

A graph is a list of nodes (skill / until / mine) plus authored edges.
Each node runs isolated. This module must not be imported by loop, agent,
stream, or display.
"""

import re
import shlex
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import agent, memory, status as status_mod
from .config import STATE_ROOT, project_dir
from .loop import (
    EVAL_PROMPT,
    UntilRun,
    _run_check,
    check_status_from_output,
    clip_check_output,
    isolated_act,
    last_assistant,
    parse_eval_status,
    parse_until_args,
    run_until,
)

NODE_KINDS = frozenset({"skill", "until", "mine"})
EDGE_ON = frozenset({"pass", "fail", "blocked"})
META_ROLE = "meta"
PAUSE_ROLE = "pause"
DONE_ROLES = frozenset({"mine", "done"})
GRAPH_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
GRAPHS_DIR = Path(__file__).parent / "graphs"
USER_GRAPHS_DIR = STATE_ROOT / "graphs"


class GraphError(Exception):
    """Invalid graph markdown or unknown graph name."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class NodeDef:
    """One graph node: skill, until, or mine."""

    name: str
    kind: str
    skill: str = ""
    task: str = ""
    goal: str = ""
    check_cmd: "str | None" = None


@dataclass(frozen=True)
class EdgeDef:
    """Authored edge. ``on`` defaults to pass."""

    src: str
    dst: str
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
        if e.dst not in names:
            raise GraphError(f"edge target {e.dst!r} is not a node")
    return GraphDef(name=name, nodes=tuple(nodes), edges=tuple(edges))


def _parse_node(tokens: list) -> NodeDef:
    if len(tokens) < 2:
        raise GraphError("node needs a name and a kind")
    name, kind = tokens[0], tokens[1]
    rest = tokens[2:]
    if kind not in NODE_KINDS:
        raise GraphError(f"unknown node kind {kind!r}")
    if kind == "mine":
        return NodeDef(name=name, kind=kind)
    if kind == "until":
        goal, check_cmd, err = parse_until_args(rest)
        if err or not goal:
            raise GraphError(f"until node {name!r} needs a goal")
        return NodeDef(name=name, kind=kind, goal=goal, check_cmd=check_cmd)
    skill, task, check_cmd = _parse_skill_rest(rest)
    return NodeDef(
        name=name, kind=kind, skill=skill, task=task, check_cmd=check_cmd,
    )


def _parse_skill_rest(words: list) -> "tuple[str, str, str | None]":
    check_cmd = None
    rest: list[str] = []
    i = 0
    while i < len(words):
        if words[i] == "--check":
            if i + 1 >= len(words):
                raise GraphError("skill node --check needs a command")
            check_cmd = words[i + 1]
            i += 2
            continue
        rest.append(words[i])
        i += 1
    if not rest:
        raise GraphError("skill node needs a skill name")
    return rest[0], " ".join(rest[1:]).strip(), check_cmd


def _parse_edge(tokens: list) -> EdgeDef:
    if len(tokens) < 3 or tokens[1] != "->":
        raise GraphError("edge needs: <from> -> <to> [on pass|fail|blocked]")
    src, dst = tokens[0], tokens[2]
    on = "pass"
    extra = tokens[3:]
    if extra:
        if extra[0] != "on" or len(extra) < 2:
            raise GraphError("edge extra tokens must be: on pass|fail|blocked")
        on = extra[1]
        if len(extra) > 2:
            raise GraphError("edge has extra tokens after on")
    if on not in EDGE_ON:
        raise GraphError(f"unknown edge on {on!r}")
    return EdgeDef(src=src, dst=dst, on=on)


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
        run._write({"ts": _now(), "role": META_ROLE, "name": name})
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
               until_run: str = "") -> None:
        step = sum(1 for e in self.events if e.get("role") not in (META_ROLE,))
        self._write({
            "ts": _now(),
            "step": step,
            "role": role,
            "status": status,
            "node": node,
            "handoff": handoff,
            "session": session,
            "until_run": until_run,
        })

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

    def next_step(self, defn: GraphDef) -> "tuple[str | None, str | None]":
        """Return (kind, node_name). kind is run, gate, or None if done."""
        last = self.last_work()
        if last is None:
            return "run", defn.start
        role, status = last.get("role"), last.get("status")
        if role == "gate":
            if status == "yes":
                return "run", last.get("node")
            return None, None
        if role in DONE_ROLES:
            return None, None
        if role == "node":
            node_name = last.get("node") or ""
            edge = defn.edge_for(node_name, status or "pass")
            if edge:
                return "run", edge.dst
            if status == "pass":
                return None, None
            return "gate", node_name
        return "run", defn.start


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
    body = agent.load_skill(node.skill, public_only=True)
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
) -> "tuple[str, str, str]":
    """Return (status, handoff, session_path). status pause means stop the graph."""
    try:
        prompt = _skill_prompt(node, handoff)
    except FileNotFoundError as e:
        echo_error(str(e))
        return "blocked", str(e), ""
    memory.record_skill_use(node.skill)
    result = isolated_act(
        cfg, model, prompt,
        confirm_gate=confirm_gate, echo=echo, echo_status=echo_status,
        echo_error=echo_error, echo_tool=echo_tool, echo_round=echo_round,
        context_limit=context_limit, context_reserve=context_reserve,
        workspace_root=workspace_root,
        log_label=f"/graph {node.name}",
    )
    if result is None:
        return "pause", "", ""
    messages, session_log = result
    summary = last_assistant(messages)
    if node.check_cmd:
        output = _run_check(cfg, node.check_cmd, confirm_gate, workspace_root)
        displayed = clip_check_output(output)
        echo_status(displayed)
        status = check_status_from_output(output)
        return status, displayed or summary, str(session_log)
    eval_prompt = EVAL_PROMPT.format(
        goal=node.task or f"complete the {node.skill} playbook",
        handoff=summary or "(none)",
        check_output="(none)",
    )
    ev = isolated_act(
        cfg, model, eval_prompt,
        confirm_gate=confirm_gate, echo=echo, echo_status=echo_status,
        echo_error=echo_error, echo_tool=echo_tool, echo_round=echo_round,
        context_limit=context_limit, context_reserve=context_reserve,
        workspace_root=workspace_root,
        log_label=f"/graph {node.name} eval",
        readonly=True,
    )
    if ev is None:
        return "pause", summary, str(session_log)
    ev_messages, _ev_session = ev
    status = parse_eval_status(last_assistant(ev_messages))
    return status, summary, str(session_log)


def _run_until_node(
    cfg: dict, model: str, node: NodeDef, handoff: str, *,
    resume_until: "str | None",
    confirm_gate, echo, echo_status, echo_error, echo_tool, echo_round,
    context_limit, context_reserve, workspace_root, ask_gate,
) -> "tuple[str, str, str, str]":
    """Return (status, handoff, session, until_path)."""
    if resume_until:
        urun = UntilRun.load(Path(resume_until))
    else:
        urun = UntilRun.create(node.goal, check_cmd=node.check_cmd)
    result = run_until(
        cfg, model, run=urun,
        confirm_gate=confirm_gate, echo=echo, echo_status=echo_status,
        echo_error=echo_error, echo_tool=echo_tool, echo_round=echo_round,
        context_limit=context_limit, context_reserve=context_reserve,
        workspace_root=workspace_root, ask_gate=ask_gate, mine=None,
        seed_handoff=handoff,
    )
    until_path = str(result.path)
    paths = result.session_paths()
    session = str(paths[-1]) if paths else ""
    summary = result.last_handoff() or result.last_maker_handoff() or ""
    if result.is_paused():
        return "pause", summary, session, until_path
    last = result.last_work()
    if last and last.get("role") == "gate" and last.get("status") == "no":
        return "blocked", summary, session, until_path
    return "pass", summary, session, until_path


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
    context_reserve: int = 2048,
    workspace_root: "Path | None" = None,
    ask_gate=None,
    mine=None,
) -> GraphRun:
    """Advance ``run`` until pass, gate-no, pause, or interrupt. Mutates run."""
    if echo_status is None:
        echo_status = echo
    root = Path(workspace_root).resolve() if workspace_root else Path.cwd().resolve()
    do_mine = bool(cfg.get("graph_mine", True)) and mine is not None
    max_steps = max(1, int(cfg.get("graph_max_steps") or 24))
    steps_this_call = 0
    current_node = ""
    current_until = ""

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
            echo_status(status_mod.msg_graph_step(
                node.name, steps_this_call, max_steps,
            ))
            handoff = run.last_handoff()
            if node.kind == "skill":
                status, summary, session = _run_skill_node(
                    cfg, model, node, handoff,
                    confirm_gate=confirm_gate, echo=echo,
                    echo_status=echo_status, echo_error=echo_error,
                    echo_tool=echo_tool, echo_round=echo_round,
                    context_limit=context_limit,
                    context_reserve=context_reserve, workspace_root=root,
                )
                if status == "pause":
                    return _pause_interrupted(run, echo_status, node=node.name)
                run.append(
                    "node", status, node=node.name,
                    handoff=summary, session=session,
                )
                continue
            if node.kind == "until":
                resume = run.resume_until(node.name)
                current_until = resume or ""
                status, summary, session, until_path = _run_until_node(
                    cfg, model, node, handoff, resume_until=resume,
                    confirm_gate=confirm_gate, echo=echo,
                    echo_status=echo_status, echo_error=echo_error,
                    echo_tool=echo_tool, echo_round=echo_round,
                    context_limit=context_limit,
                    context_reserve=context_reserve, workspace_root=root,
                    ask_gate=ask_gate,
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
                )
                continue
            raise RuntimeError(f"unknown graph node kind {node.kind!r}")
    except KeyboardInterrupt:
        return _pause_interrupted(
            run, echo_status, node=current_node, until_run=current_until,
        )

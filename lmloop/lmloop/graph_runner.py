"""Graph workflow runner: frontier scheduling and node drivers."""

from __future__ import annotations

import difflib
import re
import shlex
import shutil
from datetime import datetime, timezone
from pathlib import Path

from . import knowledge_graph, skills, snapshot, status as status_mod, tools
from .config import DEFAULTS, cfg_bool, cfg_int, utc_now
from .graph_dsl import (
    EDGE_ON,
    GRAPHS_DIR,
    GRAPH_NAME_RE,
    GraphDef,
    GraphError,
    NodeDef,
    RESERVED_GRAPH_NAMES,
    USER_GRAPHS_DIR,
    graph_path,
    parse_graph,
    reachable_from_frontier,
)
from .run_log import (
    DONE_ROLES,
    META_ROLE,
    PAUSE_ROLE,
    GraphRun as _GraphRun,
)


class GraphRun(_GraphRun):
    """Graph run log; re-exported from ``graph`` facade."""

    def join_handoff(self, node: NodeDef) -> str:
        return graph_join_handoff(self, node)

    def next_step(self, defn: GraphDef) -> "tuple[str | None, str | None]":
        return graph_next_step(self, defn)

    def frontier_state(self, defn: GraphDef) -> "tuple[list[str], str]":
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
        frontier, unrouted, latest = _replay_frontier(self, defn)
        if unrouted:
            return [], "blocked"
        names = [
            name for name in (node.name for node in defn.nodes)
            if name in frontier and _needs_satisfied(defn.node(name), latest)
        ]
        if names:
            return names, "run"
        if frontier:
            return [], "blocked"
        return [], "done"

    def runnable_names(self, defn: GraphDef) -> list[str]:
        names, _phase = self.frontier_state(defn)
        return names

    def _replay_frontier(self, defn: GraphDef):
        return _replay_frontier(self, defn)

    @classmethod
    def create(cls, name: str, slug: "str | None" = None) -> "GraphRun":
        from . import graph as graph_facade
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        d = graph_facade.project_dir(slug) / "graphs" / name
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{ts}.jsonl"
        n = 1
        while path.exists():
            path = d / f"{ts}-{n}.jsonl"
            n += 1
        run = cls(path=path, name=name, events=[])
        run._write({"ts": utc_now(), "role": META_ROLE, "name": name})
        return run
from .server import eval_model_name
from .until_runner import (
    EVAL_PROMPT,
    UntilRun,
    approved_note,
    boundary_approval,
    drop_denied,
    eval_max_rounds,
    last_assistant,
    parse_eval_status,
    run_plan_commands,
)
from .workflow_finish import finish_graph_run

JOIN_HANDOFF_CLIP = 1500
PROPOSE_MIN_TERMINAL_RUNS = 5


def graph_join_handoff(run: GraphRun, node: NodeDef) -> str:
    if not node.needs:
        return run.last_handoff()
    parts: list[str] = []
    for need in node.needs:
        summary = run.handoff_for_node(need)
        if len(summary) > JOIN_HANDOFF_CLIP:
            summary = summary[:JOIN_HANDOFF_CLIP] + "…"
        parts.append(f"[{need}]\n{summary}")
    return "\n\n".join(parts)


def _replay_frontier(
    run: GraphRun, defn: GraphDef,
) -> "tuple[set[str], list[tuple[str, str]], dict[str, str]]":
    frontier: set[str] = {defn.start}
    unrouted: list[tuple[str, str]] = []
    latest: dict[str, str] = {}
    for ev in run.events:
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


def _needs_satisfied(node: NodeDef, latest: dict[str, str]) -> bool:
    return all(latest.get(need) == "pass" for need in node.needs)


def _unsatisfiable_waits(
    run: GraphRun, defn: GraphDef, frontier: set[str], latest: dict[str, str],
) -> list[tuple[str, str]]:
    reachable = reachable_from_frontier(defn, frontier)
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


def graph_next_step(run: GraphRun, defn: GraphDef) -> "tuple[str | None, str | None]":
    last = run.last_work()
    if last is None:
        return "run", defn.start
    role, status = last.get("role"), last.get("status")
    if role == "gate":
        if status == "yes":
            node = last.get("node") or ""
            if node:
                return "run", node
        return None, None
    if role in DONE_ROLES:
        return None, None

    frontier, unrouted, latest = _replay_frontier(run, defn)
    if not frontier and not unrouted:
        return None, None

    declaration = [n.name for n in defn.nodes]
    runnable = [
        n for n in declaration
        if n in frontier and _needs_satisfied(defn.node(n), latest)
    ]
    if runnable:
        return "run", runnable[0]

    unsat = _unsatisfiable_waits(run, defn, frontier, latest)
    if unrouted or unsat:
        gate_node = unrouted[0][0] if unrouted else ""
        return "gate", gate_node
    return "gate", ""


def all_graph_runs(slug: "str | None" = None) -> list[Path]:
    from . import graph as graph_facade
    d = graph_facade.project_dir(slug) / "graphs"
    if not d.exists():
        return []
    return sorted(d.glob("*/*.jsonl"))


def graphs_dir(slug: "str | None" = None) -> Path:
    from . import graph as graph_facade
    d = graph_facade.project_dir(slug) / "graphs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def latest_open_graph_run(slug: "str | None" = None) -> "GraphRun | None":
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
    from . import graph as graph_facade
    result = graph_facade.isolated_act(cfg, model, prompt, **act_kwargs)
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
        result = graph_facade.isolated_act(cfg, model, retry_prompt, **act_kwargs)
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
        budget_block="(not an until run)",
    )
    ev = graph_facade.isolated_act(
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
    if resume_until:
        urun = UntilRun.load(Path(resume_until))
    else:
        from .config import cfg_str
        urun = UntilRun.create(
            node.goal, checks=node.checks, keeps=node.keeps,
            until_control=node.control or cfg_str(cfg, "until_control"),
        )
    from . import graph as graph_facade
    result = graph_facade.run_until(
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
    confirm_gate = tools.autonomous_gate(cfg, confirm_gate, echo_status)

    try:
        while True:
            if run.is_done():
                return run
            kind, node_name = graph_next_step(run, defn)
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
            handoff = graph_join_handoff(run, node)
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
        finish_graph_run(cfg, run, defn)


def is_packaged_graph(name: str) -> bool:
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
    out: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("node ") and " until " in stripped:
            if "--check" in stripped:
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
    from . import workflow

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
    from . import graph as graph_facade
    result = graph_facade.isolated_act(
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

"""Terminal side effects when an until or graph run returns (workflow leaf)."""

from __future__ import annotations

from ..core.config import Config
from ..memory.knowledge_graph import record_workflow_run


def until_outcome_label(events: list) -> str:
    """Coarse until-run outcome for local usage evals."""
    if not events:
        return "empty"
    last = events[-1]
    role = last.get("role")
    status = last.get("status")
    if role == "pause":
        return "paused"
    if role == "abstain":
        return "abstain"
    if role == "done" and status == "pass":
        return "pass"
    if role == "mine":
        return "pass_mined"
    if role == "gate" and status == "no":
        return "stopped"
    if role == "eval":
        return f"eval_{status}"
    if role == "check":
        return f"check_{status}"
    return f"{role}:{status}"


def finish_until_run(cfg: Config, run, *, check_commands: list[str]) -> None:
    """Usage + knowledge-graph hooks after ``run_until`` returns."""
    from ..core import usage

    usage.record(
        "until.finish",
        outcome=until_outcome_label(run.events),
        paused=run.is_paused(),
        done=run.is_done(),
    )
    record_workflow_run(
        cfg,
        run_key=run.path.stem,
        goal=run.goal or "",
        commands=check_commands,
    )


def finish_graph_run(cfg: Config, run, defn) -> None:
    """Knowledge-graph hook after ``run_graph`` returns."""
    cmds: list[str] = []
    for node in defn.nodes:
        cmds.extend(list(node.checks or ()))
        cmds.extend(list(node.keeps or ()))
    record_workflow_run(
        cfg,
        run_key=f"{run.name}:{run.path.stem}",
        goal=run.name,
        commands=cmds,
    )

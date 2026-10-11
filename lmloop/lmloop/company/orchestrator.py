"""Host orchestrator: frontier, worktrees, workers, milestone gates.

``graph`` and ``loop`` do not import this module. Parallelism is capped at two
worker nodes with distinct worktrees, and only when company mode is on.
"""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from .. import memory
from ..config import cfg_int, utc_now
from ..server import resolve_model_concurrency
from .allowlist import endpoint_error, effective_allowlist
from .manifest import MANIFEST_REL, Manifest, ManifestError, allowlist_errors, load_manifest
from .protocol import envelope
from .worktree import WorktreeError, ensure_isolation, head_sha, merge_into

COMPANY_MAX_PARALLEL = 2
HOST_SKILLS = frozenset({"ceo", "retro", "plan", "learn"})
PREFIX_HEADING = "company prefix"


class CompanyError(RuntimeError):
    """Company mode refused to start or could not finish a milestone."""


@dataclass
class CompanyResult:
    status: str
    run_path: Path
    batches: list = field(default_factory=list)
    milestone: str = ""
    summary: str = ""


def cache_prefix(*, skill_text: str, manifest_sha: str, checks: list,
                 memory_block: str) -> str:
    """Tier A+B text. Handoffs and goals stay out so the prefix can be cached."""
    lines = [skill_text or "", f"manifest {manifest_sha}"]
    for check in checks:
        if isinstance(check, dict):
            lines.append(str(check.get("cmd") or ""))
        else:
            lines.append(str(check))
    lines.append(memory_block or "")
    return "\n".join(lines)


def prefix_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def parallel_cap(cfg: dict, manifest: Manifest) -> int:
    """At most two workers. A single local slot still runs one worker at a time."""
    slots = resolve_model_concurrency(cfg)
    reserve = 1 if slots > 1 else 0
    room = max(1, slots - reserve)
    return max(1, min(COMPANY_MAX_PARALLEL, manifest.max_parallel_workers, room))


def _is_host(node) -> bool:
    if node.kind == "mine":
        return True
    return node.kind == "skill" and node.skill in HOST_SKILLS


def _run_dir(run_id: str) -> Path:
    path = memory.project_dir() / "company" / run_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _append(path: Path, row: dict) -> None:
    memory.append_jsonl(path, row)


def activation_errors(cfg: dict, manifest: Manifest, *, sandbox_ready: bool) -> list[str]:
    errors = []
    if not sandbox_ready:
        errors.append("company run needs --docker or --docker-persist")
    endpoint = endpoint_error(cfg)
    if endpoint:
        errors.append(endpoint)
    errors.extend(allowlist_errors(manifest, set(effective_allowlist(cfg))))
    return errors


def _launch_many(launcher, packets: list) -> list:
    if len(packets) <= 1:
        return [launcher(packets[0])] if packets else []
    with ThreadPoolExecutor(max_workers=len(packets)) as pool:
        futures = [pool.submit(launcher, packet) for packet in packets]
        return [item.result() for item in futures]


def _packet(manifest: Manifest, *, run_id: str, node, role, model: str, handoff: str,
            worktree: str, base_sha: str, campaign_id: str, plan_revision: int,
            plan_block: str, memory_block: str, token_budget: int,
            board: str = "") -> dict:
    checks = [{"cmd": cmd, "role": "check"} for cmd in (node.checks or ())]
    skill_text = ""
    if node.kind == "skill" and node.skill:
        from ..skills import load_skill
        try:
            skill_text = load_skill(node.skill, public_only=True)
        except FileNotFoundError:
            skill_text = node.skill
    prefix = cache_prefix(
        skill_text=skill_text, manifest_sha=manifest.sha,
        checks=checks, memory_block=memory_block,
    )
    goal = node.goal or node.task or node.skill or node.name
    return {
        "manifest_sha": manifest.sha,
        "manifest_path": str(manifest.path),
        "run_id": run_id,
        "node": node.name,
        "graph": manifest.graph,
        "worktree": worktree,
        "base_sha": base_sha,
        "model": model,
        "mode": "mine" if node.kind == "mine" else ("until" if node.kind == "until" else "skill"),
        "goal": goal,
        "handoff": handoff,
        "checks": checks,
        "budgets": {"max_rounds": 40, "token_budget": token_budget},
        "board_clip": board,
        "campaign_id": campaign_id,
        "plan_revision": plan_revision,
        "plan_block": plan_block,
        "prefix_hash": prefix_hash(prefix),
        "host": _is_host(node),
    }


def run_company(
    cfg: dict, *, repo: Path, goal: str, sandbox_ready: bool,
    launcher, manifest: "Manifest | None" = None,
    manifest_path: "Path | None" = None,
    campaign_id: str = "",
    gate_runner=None,
    harvester=None,
    defn=None,
) -> CompanyResult:
    """Drive one company run. ``launcher(packet) -> envelope`` stands in for a worker."""
    from ..exec import workspace_rejection
    from ..graph import GraphError, GraphRun, load_graph

    root = repo.resolve()
    rejection = workspace_rejection(root)
    if rejection:
        raise CompanyError(rejection)
    path = manifest_path or (root / MANIFEST_REL)
    try:
        loaded = manifest or load_manifest(path)
    except ManifestError as exc:
        raise CompanyError(str(exc)) from exc
    errors = activation_errors(cfg, loaded, sandbox_ready=sandbox_ready)
    if errors:
        raise CompanyError("; ".join(errors))
    try:
        graph = defn or load_graph(loaded.graph)
    except GraphError as exc:
        raise CompanyError(str(exc)) from exc
    missing = [node.name for node in graph.nodes if loaded.role_for_node(node.name) is None]
    if missing:
        raise CompanyError("no manifest role for " + ", ".join(missing))
    try:
        base = head_sha(root)
    except WorktreeError as exc:
        raise CompanyError(str(exc)) from exc

    run_id = "company-" + utc_now().replace(":", "").replace("-", "")
    log_path = _run_dir(run_id) / "run.jsonl"
    _append(log_path, {
        "ts": utc_now(), "role": "meta", "goal": goal,
        "manifest_sha": loaded.sha, "base_sha": base, "graph": loaded.graph,
    })
    worktrees: dict[str, Path] = {}
    for role in loaded.roles:
        if role.worktree == "_integration":
            raise CompanyError("worktree name _integration is reserved")
        if not role.worktree:
            continue
        try:
            worktrees[role.worktree] = ensure_isolation(
                root, role.worktree, base, loaded.isolation,
            )
        except WorktreeError as exc:
            raise CompanyError(str(exc)) from exc
    integration = None
    if loaded.milestones or any(role.worktree for role in loaded.roles):
        try:
            integration = ensure_isolation(root, "_integration", base, "worktree")
        except WorktreeError as exc:
            raise CompanyError(str(exc)) from exc

    plan_text = ""
    plan_revision = 0
    if campaign_id:
        from ..campaign import attach_run, plan_block, revision_of
        plan_revision = revision_of(campaign_id) or 0
        plan_text = plan_block(campaign_id, role="")
        attach_run(campaign_id, kind="company", path=str(log_path))

    graph_run = GraphRun.create(loaded.graph)
    cap = parallel_cap(cfg, loaded)
    batches: list[list[str]] = []
    steps = 0
    max_steps = max(1, cfg_int(cfg, "graph_max_steps"))
    status = "pass"
    finished = False
    while steps < max_steps:
        names, phase = graph_run.frontier_state(graph)
        if phase == "done":
            finished = True
            break
        if phase == "blocked":
            status = "blocked"
            break
        if phase != "run" or not names:
            break
        host = [name for name in names if _is_host(graph.node(name))]
        workers = [name for name in names if name not in host]
        if host:
            chosen = [host[0]]
        else:
            chosen = []
            seen: set[str] = set()
            for name in workers:
                role = loaded.role_for_node(name)
                wt = role.worktree if role else ""
                if wt in seen:
                    continue
                seen.add(wt)
                chosen.append(name)
                if len(chosen) >= cap:
                    break
        packets = []
        for name in chosen:
            node = graph.node(name)
            role = loaded.role_for_node(name)
            model = loaded.model_for(name, role) or cfg.get("model") or ""
            wt_path = ""
            if role and role.worktree:
                wt_path = str(worktrees[role.worktree])
            role_name = role.name if role else ""
            block = plan_text
            board = ""
            if campaign_id and role_name:
                from ..campaign import board_clip, plan_block
                block = plan_block(campaign_id, role=role_name)
                board = board_clip(campaign_id, role=role_name)
            elif campaign_id:
                from ..campaign import board_clip
                board = board_clip(campaign_id, role=name)
            packets.append(_packet(
                loaded, run_id=run_id, node=node, role=role, model=model,
                handoff=graph_run.join_handoff(node),
                worktree=wt_path, base_sha=base if wt_path else "",
                campaign_id=campaign_id, plan_revision=plan_revision,
                plan_block=block, memory_block="",
                token_budget=cfg_int(cfg, "run_token_budget"),
                board=board,
            ))
        batches.append([item["node"] for item in packets])
        for item in packets:
            _append(log_path, {
                "ts": utc_now(), "role": "prefix", "node": item["node"],
                "prefix_hash": item["prefix_hash"],
            })
            if campaign_id:
                from ..campaign import append_board
                append_board(campaign_id, "spawn", role=item["node"], text=item["goal"])
        envelopes = _launch_many(launcher, packets)
        for packet, result in zip(packets, envelopes):
            if not isinstance(result, dict):
                result = envelope("blocked", "worker returned no envelope")
            node_status = result.get("status") or "blocked"
            if node_status not in ("pass", "fail", "blocked", "pause"):
                node_status = "blocked"
            _append(log_path, {
                "ts": utc_now(), "role": "worker", "node": packet["node"],
                "status": node_status, "summary": result.get("summary") or "",
                "denied": result.get("denied") or [],
            })
            if campaign_id:
                from ..campaign import append_board
                kind = "blocker" if node_status in ("blocked", "fail") else "status"
                append_board(
                    campaign_id, kind, role=packet["node"],
                    text=str(result.get("summary") or node_status),
                )
            if node_status == "pause":
                graph_run.append("pause", "paused", node=packet["node"])
                status = "paused"
                break
            graph_run.append(
                "node", node_status, node=packet["node"],
                handoff=str(result.get("summary") or ""),
                session=str(result.get("session_log") or ""),
            )
            if node_status == "pass" and packet.get("worktree") and integration is not None:
                ok, detail = merge_into(integration, Path(packet["worktree"]))
                _append(log_path, {
                    "ts": utc_now(), "role": "merge", "node": packet["node"],
                    "status": "pass" if ok else "blocked", "summary": detail,
                })
                if not ok:
                    status = "blocked"
        steps += len(packets)
        if status in ("paused", "blocked"):
            break
    if status == "pass" and not finished:
        _names, phase = graph_run.frontier_state(graph)
        if phase == "done":
            finished = True
        elif phase == "run":
            status = "paused"

    milestone_status = ""
    if finished and status == "pass":
        milestone_status, status = _milestones(
            cfg, loaded, integration or root, log_path, gate_runner, harvester, goal,
            campaign_id=campaign_id,
        )
        if status == "pass":
            graph_run.append("done", "pass")
    _append(log_path, {"ts": utc_now(), "role": "done", "status": status, "goal": goal})
    return CompanyResult(
        status=status, run_path=log_path, batches=batches,
        milestone=milestone_status, summary=goal,
    )


def _milestones(cfg, manifest: Manifest, integration: Path, log_path: Path,
                gate_runner, harvester, goal: str, *, campaign_id: str) -> "tuple[str, str]":
    if gate_runner is None:
        gate_runner = _default_gate
    passed = []
    for milestone in manifest.milestones:
        for gate in milestone.gates:
            result = gate_runner(gate.cmd, integration)
            ok = isinstance(result, dict) and result.get("status") == "pass"
            _append(log_path, {
                "ts": utc_now(), "role": "milestone", "node": milestone.id,
                "status": "pass" if ok else "fail",
                "summary": (result or {}).get("output") if isinstance(result, dict) else "",
            })
            if not ok:
                return milestone.id, "fail"
        passed.append(milestone.id)
        if milestone.on_pass and harvester is not None:
            harvester({"milestone": milestone.id, "goal": goal, "cfg": cfg})
        if campaign_id:
            from ..campaign import reflect
            reflect(campaign_id, "milestone", {"milestone": milestone.id, "goal": goal})
    if not manifest.milestones and harvester is not None:
        harvester({"milestone": "", "goal": goal, "cfg": cfg})
    return ",".join(passed), "pass"


def _default_gate(cmd: str, cwd: Path) -> dict:
    import shlex
    import subprocess
    try:
        argv = shlex.split(cmd)
    except ValueError as exc:
        return {"status": "blocked", "output": str(exc)}
    if not argv:
        return {"status": "blocked", "output": "empty gate"}
    try:
        proc = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, timeout=120, check=False,
        )
    except OSError as exc:
        return {"status": "blocked", "output": str(exc)}
    output = ((proc.stdout or "") + (proc.stderr or ""))[-2000:]
    if proc.returncode == 0:
        return {"status": "pass", "output": output}
    return {"status": "fail", "output": output}


def read_company_goal(words: list) -> "tuple[str, str]":
    """Parse ``--goal TEXT`` and ``--campaign ID`` from a company run tail."""
    goal = ""
    campaign = ""
    idx = 0
    while idx < len(words):
        token = words[idx]
        if token == "--goal" and idx + 1 < len(words):
            goal = words[idx + 1]
            idx += 2
            continue
        if token == "--campaign" and idx + 1 < len(words):
            campaign = words[idx + 1]
            idx += 2
            continue
        if not goal:
            goal = token
        idx += 1
    return goal, campaign

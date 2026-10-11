"""Headless company worker. Stdin is one task packet; stdout is one result envelope.

Workers do not append project memory. The orchestrator ingests summaries after gates.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from .allowlist import activate, effective_allowlist
from .manifest import ManifestError, canonical_sha, load_manifest
from .protocol import PacketError, envelope, parse_packet
from .worktree import WorktreeError, head_sha


def _manifest_sha(path: Path) -> str:
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise PacketError("manifest is not an object")
    return canonical_sha(data)


def _plan_revision(campaign_id: str) -> "int | None":
    if not campaign_id:
        return None
    from ..campaign import revision_of
    return revision_of(campaign_id)


def validate_packet(packet: dict, cfg: dict) -> "str | None":
    """Return a blocked reason, or None when the packet may run."""
    try:
        parse_packet(packet)
    except PacketError as exc:
        return str(exc)
    model = str(packet.get("model") or "")
    if model not in set(effective_allowlist(cfg)):
        return f"model {model!r} is not on the company allowlist"
    path = Path(str(packet.get("manifest_path")))
    try:
        digest = _manifest_sha(path)
    except (OSError, json.JSONDecodeError, PacketError) as exc:
        return f"manifest unreadable: {exc}"
    if digest != packet.get("manifest_sha"):
        return "manifest_sha does not match the manifest on disk"
    try:
        loaded = load_manifest(path)
    except ManifestError as exc:
        return str(exc)
    if loaded.sha != packet.get("manifest_sha"):
        return "manifest_sha does not match the manifest on disk"
    worktree = str(packet.get("worktree") or "")
    base = str(packet.get("base_sha") or "")
    if worktree and base:
        try:
            current = head_sha(Path(worktree))
        except WorktreeError as exc:
            return str(exc)
        if current != base:
            return "worktree HEAD is not the pinned base_sha"
    campaign_id = str(packet.get("campaign_id") or "")
    if campaign_id:
        current_rev = _plan_revision(campaign_id)
        wanted = packet.get("plan_revision")
        if current_rev is None or int(wanted or 0) != int(current_rev):
            return "stale plan_revision"
    return None


def execute_packet(packet: dict, cfg: dict, *, executor=None) -> dict:
    """Validate, then run ``executor`` or one graph node. Never writes learnings."""
    reason = validate_packet(packet, cfg)
    if reason:
        return envelope("blocked", reason)
    active = activate(cfg, orchestrator=False)
    if executor is not None:
        result = executor(packet, active)
        if isinstance(result, dict) and result.get("status"):
            return result
        return envelope("blocked", "worker executor returned no envelope")
    return _run_node(packet, active)


def _run_node(packet: dict, cfg: dict) -> dict:
    from ..graph import GraphError, load_graph, run_named_node

    try:
        defn = load_graph(str(packet.get("graph") or "company"))
    except GraphError as exc:
        return envelope("blocked", str(exc))
    root = Path(str(packet.get("worktree") or "")) if packet.get("worktree") else Path.cwd()
    outcome = run_named_node(
        cfg, str(packet.get("model")), defn, str(packet.get("node")),
        str(packet.get("handoff") or ""),
        workspace_root=root,
    )
    return envelope(
        outcome.get("status") or "blocked",
        outcome.get("summary") or "",
        session_log=outcome.get("session") or "",
    )


def spawn_worker(packet: dict) -> dict:
    """Run one worker process. Stdout must be an envelope; stderr is diagnostics."""
    from .protocol import PacketError, parse_envelope

    cmd = [sys.executable, "-m", "lmloop", "--docker", "worker", "run"]
    cwd = str(packet.get("worktree") or "") or None
    budgets = packet.get("budgets") if isinstance(packet.get("budgets"), dict) else {}
    timeout = int(budgets.get("timeout_s") or 600)
    try:
        proc = subprocess.run(
            cmd, input=json.dumps(packet), text=True, capture_output=True,
            cwd=cwd, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return envelope("blocked", str(exc))
    try:
        return parse_envelope(proc.stdout)
    except PacketError:
        detail = (proc.stderr or proc.stdout or "worker failed").strip()
        return envelope("blocked", detail[-500:])


def main_worker(cfg: dict, stdin: "str | None" = None) -> int:
    """Read one packet from stdin and print one envelope. Used by ``lmloop worker run``."""
    raw = sys.stdin.read() if stdin is None else stdin
    try:
        data = json.loads(raw or "")
    except json.JSONDecodeError as exc:
        print(json.dumps(envelope("blocked", f"packet is not JSON: {exc}")))
        return 0
    if not isinstance(data, dict):
        print(json.dumps(envelope("blocked", "packet must be a JSON object")))
        return 0
    print(json.dumps(execute_packet(data, cfg)))
    return 0

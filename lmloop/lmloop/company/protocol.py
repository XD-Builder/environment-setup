"""Task packet and result envelope. Workers return this; they do not append JSONL."""

from __future__ import annotations

import json

REQUIRED = (
    "manifest_sha", "manifest_path", "run_id", "node", "model", "mode", "goal",
)
STATUSES = frozenset({"pass", "fail", "blocked", "pause"})


class PacketError(ValueError):
    """The task packet or result envelope is not usable."""


def parse_packet(data: dict) -> dict:
    if not isinstance(data, dict):
        raise PacketError("task packet must be a JSON object")
    missing = [key for key in REQUIRED if not str(data.get(key) or "").strip()]
    if missing:
        raise PacketError("task packet missing " + ", ".join(missing))
    mode = str(data.get("mode"))
    if mode not in ("until", "skill", "mine", "host"):
        raise PacketError(f"unsupported packet mode {mode!r}")
    checks = data.get("checks") or []
    if not isinstance(checks, list):
        raise PacketError("checks must be a list")
    budgets = data.get("budgets") or {}
    if not isinstance(budgets, dict):
        raise PacketError("budgets must be an object")
    return data


def envelope(status: str, summary: str, *, tree_sha: str = "", usage: "dict | None" = None,
             denied: "list | None" = None, session_log: str = "") -> dict:
    if status not in STATUSES:
        raise PacketError(f"unknown envelope status {status!r}")
    return {
        "status": status,
        "summary": summary or "",
        "tree_sha": tree_sha or "",
        "usage": usage or {},
        "denied": list(denied or []),
        "session_log": session_log or "",
    }


def parse_envelope(stdout: str) -> dict:
    """Last JSON object on stdout. Anything else is a blocked envelope."""
    found = None
    for line in (stdout or "").splitlines():
        text = line.strip()
        if not text.startswith("{"):
            continue
        try:
            row = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("status") in STATUSES:
            found = row
    if found is None:
        raise PacketError("worker stdout had no result envelope")
    return envelope(
        str(found.get("status")),
        str(found.get("summary") or ""),
        tree_sha=str(found.get("tree_sha") or ""),
        usage=found.get("usage") if isinstance(found.get("usage"), dict) else {},
        denied=found.get("denied") if isinstance(found.get("denied"), list) else [],
        session_log=str(found.get("session_log") or ""),
    )

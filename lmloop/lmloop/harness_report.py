"""Opt-in harness execution report from until/graph JSONL (H2)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import memory
from .config import Config, project_slug, utc_now
from .evals import feedback_case_ids
from .harness_bundle import bundle_id
from .run_log import META_ROLE

_FAILURE_ROLES = frozenset({"check", "eval", "gate", "blocked"})
_CLIP = 240


def _clip(text: str, limit: int = _CLIP) -> str:
    body = (text or "").replace("\n", " ").strip()
    if len(body) <= limit:
        return body
    return body[: limit - 1] + "…"


def _aggregate(events: list[dict]) -> dict[str, int]:
    counts = {
        "steps": 0,
        "checks_fail": 0,
        "checks_pass": 0,
        "eval_fail": 0,
        "eval_pass": 0,
        "gate_no": 0,
    }
    for row in events:
        role = str(row.get("role") or "")
        if role == META_ROLE:
            continue
        counts["steps"] += 1
        status = str(row.get("status") or "")
        if role == "check":
            if status in ("pass", "ok"):
                counts["checks_pass"] += 1
            elif status in ("fail", "blocked"):
                counts["checks_fail"] += 1
        elif role == "eval":
            if status in ("pass", "ok"):
                counts["eval_pass"] += 1
            elif status in ("fail",):
                counts["eval_fail"] += 1
        elif role == "gate" and status == "no":
            counts["gate_no"] += 1
    return counts


def _failures(events: list[dict], *, limit: int = 8) -> list[dict]:
    out: list[dict] = []
    for row in events:
        role = str(row.get("role") or "")
        if role not in _FAILURE_ROLES:
            continue
        status = str(row.get("status") or "")
        if role == "gate" and status != "no":
            continue
        if role in ("check", "eval") and status not in ("fail", "blocked"):
            continue
        entry: dict[str, Any] = {
            "role": role,
            "status": status,
            "step": row.get("step"),
        }
        cmd = row.get("cmd") or row.get("check_cmd")
        if cmd:
            entry["id"] = str(cmd)
        handoff = row.get("handoff") or ""
        results = row.get("results")
        excerpt = handoff
        if isinstance(results, list) and results:
            last = results[-1]
            if isinstance(last, dict):
                excerpt = str(last.get("output") or last.get("stderr") or handoff)
        entry["excerpt"] = _clip(excerpt)
        out.append(entry)
        if len(out) >= limit:
            break
    return out


def _mode_from_path(path: Path) -> str:
    text = path.as_posix()
    if "/until/" in text or text.endswith("/until.jsonl") or "/fixtures/harness_report/" in text:
        return "until"
    if "/graphs/" in text:
        return "graph"
    return "unknown"


def build_harness_execution_report(
    run_path: Path,
    *,
    cfg: Config | None = None,
    solver: str = "",
) -> dict[str, Any]:
    """Build a report dict from one until or graph JSONL file."""
    events = memory.read_jsonl(run_path)
    meta = events[0] if events and events[0].get("role") == META_ROLE else {}
    goal = str(meta.get("goal") or meta.get("name") or "")
    checks = meta.get("checks") if isinstance(meta.get("checks"), list) else None
    bundle = str(meta.get("harness_bundle_id") or "")
    if not bundle:
        graph_name = str(meta.get("name") or run_path.parent.name)
        bundle = bundle_id(cfg, graph_name=graph_name, checks=checks)
    return {
        "ts": utc_now(),
        "run_path": str(run_path),
        "task": {
            "mode": _mode_from_path(run_path),
            "goal": goal,
            "project": project_slug(),
        },
        "harness_bundle_id": bundle,
        "solver": solver or "",
        "aggregate": _aggregate(events),
        "failures": _failures(events),
        "question_sets": {"feedback": feedback_case_ids()},
    }


def write_harness_execution_report(
    run_path: Path,
    *,
    cfg: Config | None = None,
    solver: str = "",
    dest: Path | None = None,
) -> Path:
    from .evals import harness_report_path

    payload = build_harness_execution_report(run_path, cfg=cfg, solver=solver)
    path = dest or harness_report_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def latest_run_path(slug: str | None = None) -> Path | None:
    from .run_log import all_graph_runs, all_until_runs

    candidates: list[Path] = list(all_until_runs(slug)) + list(all_graph_runs(slug))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item.stat().st_mtime)


def format_harness_report(report: dict) -> str:
    agg = report.get("aggregate") or {}
    lines = [
        "lmloop eval harness-report",
        f"  run: {report.get('run_path', '')}",
        f"  harness_bundle_id: {report.get('harness_bundle_id', '')}",
        f"  task: {(report.get('task') or {}).get('goal', '')}",
        "  aggregate:",
        f"    checks fail/pass: {agg.get('checks_fail', 0)}/{agg.get('checks_pass', 0)}",
        f"    eval fail/pass: {agg.get('eval_fail', 0)}/{agg.get('eval_pass', 0)}",
        f"    gate_no: {agg.get('gate_no', 0)}",
    ]
    failures = report.get("failures") or []
    if failures:
        lines.append(f"  failures ({len(failures)}):")
        for row in failures:
            lines.append(f"    - {row.get('role')}/{row.get('status')}: {row.get('excerpt', '')}")
    else:
        lines.append("  failures: none")
    return "\n".join(lines)

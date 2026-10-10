"""Local eval signals from ``usage.jsonl`` — aggregate, validate, surface gaps.

Read-only analysis; no network. Feeds human and agent improvement loops
(see ``docs/DESIGN_USAGE_EVALS_AND_SELF_IMPROVEMENT.md``).
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .usage import USAGE_PATH, read_events

# Every ``usage.record("feature", ...)`` stem should appear here (tests enforce).
KNOWN_FEATURES = frozenset({
    "agent.act",
    "agent.act.finish",
    "check.cycle",
    "cli.command",
    "cli.repl",
    "graph.run",
    "repl.command",
    "repl.session",
    "tool",
    "tool.error",
    "until.finish",
    "until.run",
})

_ROW_TS = "ts"
_ROW_FEATURE = "feature"
_ROW_DETAIL = "detail"

# Minimum events before a gap is promoted from "watch" to "action".
_DEFAULT_MIN_SAMPLES = 8
_TOOL_ERROR_RATE = 0.25
_HIGH_ROUNDS = 12


@dataclass(frozen=True)
class UsageRowError:
    index: int
    message: str


@dataclass
class EvalStats:
    """Aggregated counters from usage JSONL."""

    events: int = 0
    invalid_rows: int = 0
    features: Counter = field(default_factory=Counter)
    tools: Counter = field(default_factory=Counter)
    tool_errors: Counter = field(default_factory=Counter)
    until_outcomes: Counter = field(default_factory=Counter)
    check_statuses: Counter = field(default_factory=Counter)
    act_rounds: list[int] = field(default_factory=list)
    act_interrupted: int = 0
    act_finished: int = 0
    first_ts: str = ""
    last_ts: str = ""
    never_pass_checks: list[str] = field(default_factory=list)
    always_pause_nodes: list[str] = field(default_factory=list)
    shell_errors: int = 0


@dataclass(frozen=True)
class EvalGap:
    """One improvement opportunity derived from local usage."""

    id: str
    severity: str  # watch | action
    title: str
    evidence: str
    design_hook: str  # which DESIGN_* doc or section to extend


def validate_row(row: dict, *, index: int = 0) -> UsageRowError | None:
    if not isinstance(row, dict):
        return UsageRowError(index, "row is not an object")
    if _ROW_TS not in row or not str(row[_ROW_TS]).strip():
        return UsageRowError(index, "missing ts")
    feature = row.get(_ROW_FEATURE)
    if not feature or not isinstance(feature, str):
        return UsageRowError(index, "missing feature")
    if feature not in KNOWN_FEATURES:
        return UsageRowError(index, f"unknown feature {feature!r}")
    detail = row.get(_ROW_DETAIL)
    if detail is not None and not isinstance(detail, dict):
        return UsageRowError(index, "detail must be an object")
    return None


def aggregate(events: list[dict]) -> EvalStats:
    stats = EvalStats()
    for i, row in enumerate(events):
        err = validate_row(row, index=i)
        if err:
            stats.invalid_rows += 1
            continue
        stats.events += 1
        ts = str(row[_ROW_TS])
        if not stats.first_ts:
            stats.first_ts = ts
        stats.last_ts = ts
        feature = row[_ROW_FEATURE]
        stats.features[feature] += 1
        detail = row.get(_ROW_DETAIL) or {}
        if feature == "tool":
            name = str(detail.get("name") or "")
            if name:
                stats.tools[name] += 1
        elif feature == "tool.error":
            name = str(detail.get("name") or "")
            if name:
                stats.tool_errors[name] += 1
        elif feature == "until.finish":
            outcome = str(detail.get("outcome") or "unknown")
            stats.until_outcomes[outcome] += 1
        elif feature == "check.cycle":
            status = str(detail.get("status") or "unknown")
            stats.check_statuses[status] += 1
        elif feature == "agent.act.finish":
            stats.act_finished += 1
            if detail.get("interrupted"):
                stats.act_interrupted += 1
            rounds = detail.get("rounds")
            if isinstance(rounds, int):
                stats.act_rounds.append(rounds)
            elif isinstance(rounds, (float, str)):
                try:
                    stats.act_rounds.append(int(rounds))
                except ValueError:
                    pass
    return stats


def find_gaps(stats: EvalStats, *, min_samples: int = _DEFAULT_MIN_SAMPLES) -> list[EvalGap]:
    gaps: list[EvalGap] = []
    if stats.events < 1:
        gaps.append(EvalGap(
            id="no-usage",
            severity="watch",
            title="No usage events yet",
            evidence="usage.jsonl is empty or all rows invalid",
            design_hook="DESIGN_USAGE_EVALS_AND_SELF_IMPROVEMENT.md § bootstrap",
        ))
        return gaps

    tool_calls = sum(stats.tools.values())
    tool_errs = sum(stats.tool_errors.values())
    if tool_calls >= min_samples and tool_errs / max(tool_calls, 1) >= _TOOL_ERROR_RATE:
        top = stats.tool_errors.most_common(3)
        gaps.append(EvalGap(
            id="tool-error-rate",
            severity="action",
            title="Tool calls fail often — tighten validation or UX",
            evidence="tool.error / tool = "
            f"{tool_errs}/{tool_calls}; top: {top}",
            design_hook="DESIGN_SANDBOX_AND_VERIFICATION.md § gates; tools.py ToolDef",
        ))

    until_total = sum(stats.until_outcomes.values())
    paused = stats.until_outcomes.get("paused", 0)
    if until_total >= min_samples and paused / until_total >= 0.4:
        gaps.append(EvalGap(
            id="until-pause-heavy",
            severity="action",
            title="Until runs pause more than they finish",
            evidence=f"until.finish paused={paused} of {until_total}",
            design_hook="DESIGN_SANDBOX_AND_VERIFICATION.md § derived checks; DESIGN_ROADMAP.md",
        ))

    if stats.act_finished >= min_samples:
        high = sum(1 for r in stats.act_rounds if r >= _HIGH_ROUNDS)
        if high / max(len(stats.act_rounds), 1) >= 0.3:
            gaps.append(EvalGap(
                id="act-high-rounds",
                severity="watch",
                title="Many turns hit high gather round counts",
                evidence=f"{high}/{len(stats.act_rounds)} act.finish rounds >= {_HIGH_ROUNDS}",
                design_hook="DESIGN_LLM_CALLING.md § gather budget; stream halt",
            ))
        if stats.act_interrupted / stats.act_finished >= 0.2:
            gaps.append(EvalGap(
                id="act-interrupted",
                severity="watch",
                title="Frequent interrupted agent turns",
                evidence=f"interrupted={stats.act_interrupted} of {stats.act_finished} act.finish",
                design_hook="DESIGN_LLM_CALLING.md § rollback; display/stream",
            ))

    cli = stats.features.get("cli.command", 0)
    repl = stats.features.get("cli.repl", 0) + stats.features.get("repl.session", 0)
    if repl >= min_samples and cli == 0:
        gaps.append(EvalGap(
            id="cli-unused",
            severity="watch",
            title="REPL-only usage — CLI subcommands never run",
            evidence=f"repl sessions ~{repl}, cli.command=0",
            design_hook="DESIGN_COMMAND_CONSOLIDATION.md; README command tables",
        ))

    unknown_features = {
        f for f in stats.features if f not in KNOWN_FEATURES
    }
    if unknown_features:
        gaps.append(EvalGap(
            id="unknown-features",
            severity="action",
            title="Usage rows reference features not in KNOWN_FEATURES",
            evidence=", ".join(sorted(unknown_features)),
            design_hook="evals.KNOWN_FEATURES + DEVELOPMENT.md instrumentation",
        ))

    if stats.never_pass_checks:
        shown = ", ".join(stats.never_pass_checks[:5])
        gaps.append(EvalGap(
            id="check-never-passes",
            severity="action",
            title="Check commands on this project never pass",
            evidence=f"commands with no pass: {shown}",
            design_hook="DESIGN_SANDBOX_AND_VERIFICATION.md § derived checks",
        ))
    if stats.always_pause_nodes:
        shown = ", ".join(stats.always_pause_nodes[:5])
        gaps.append(EvalGap(
            id="graph-node-always-pauses",
            severity="action",
            title="Graph nodes always pause",
            evidence=f"nodes: {shown}",
            design_hook="DESIGN_DAG_AND_KNOWLEDGE_CANVAS.md § fan-out",
        ))
    shell_errs = stats.tool_errors.get("run_shell", 0) or stats.shell_errors
    paused = stats.until_outcomes.get("paused", 0)
    if shell_errs >= 3 and paused >= 1:
        gaps.append(EvalGap(
            id="shell-error-before-pause",
            severity="watch",
            title="run_shell errors coincide with paused until runs",
            evidence=f"tool.error run_shell={shell_errs}; until paused={paused}",
            design_hook="DESIGN_USAGE_EVALS_AND_SELF_IMPROVEMENT.md § V1",
        ))

    return gaps


_SECRET_CMD = re.compile(
    r"(?i)(api[_-]?key|secret|token|password)\s*=",
)


def _safe_cmd(cmd: str) -> str:
    text = (cmd or "").strip()
    if not text or _SECRET_CMD.search(text):
        return ""
    return text[:180]


def join_run_logs(stats: EvalStats, slug: "str | None" = None) -> EvalStats:
    """Fold until/graph JSONL into gap inputs. Commands with secrets are dropped."""
    from .config import project_dir
    from .memory import read_jsonl

    root = project_dir(slug)
    seen: Counter = Counter()
    passed: Counter = Counter()
    until_dir = root / "until"
    if until_dir.is_dir():
        for path in sorted(until_dir.glob("*.jsonl")):
            for row in read_jsonl(path):
                if row.get("role") not in ("check", "baseline"):
                    continue
                for result in row.get("results") or []:
                    cmd = _safe_cmd(str(result.get("cmd") or ""))
                    if not cmd:
                        continue
                    seen[cmd] += 1
                    if result.get("status") == "pass":
                        passed[cmd] += 1
    stats.never_pass_checks = sorted(
        cmd for cmd, count in seen.items() if count >= 2 and passed[cmd] == 0
    )
    pauses: Counter = Counter()
    oks: Counter = Counter()
    graphs = root / "graphs"
    if graphs.is_dir():
        for path in sorted(graphs.glob("*/*.jsonl")):
            for row in read_jsonl(path):
                name = str(row.get("node") or "")
                if not name:
                    continue
                status = str(row.get("status") or "")
                role = str(row.get("role") or "")
                if status in ("pause", "paused") or role == "pause":
                    pauses[name] += 1
                elif status == "pass":
                    oks[name] += 1
    stats.always_pause_nodes = sorted(
        name for name, count in pauses.items() if count >= 2 and oks[name] == 0
    )
    return stats


def load_stats(path: Path | None = None, *, slug: "str | None" = None) -> EvalStats:
    stats = aggregate(read_events(path or USAGE_PATH))
    try:
        join_run_logs(stats, slug)
    except OSError:
        pass
    return stats


def report_dict(stats: EvalStats, gaps: list[EvalGap]) -> dict[str, Any]:
    return {
        "events": stats.events,
        "invalid_rows": stats.invalid_rows,
        "range": {"first_ts": stats.first_ts, "last_ts": stats.last_ts},
        "features": dict(stats.features),
        "tools": dict(stats.tools),
        "tool_errors": dict(stats.tool_errors),
        "until_outcomes": dict(stats.until_outcomes),
        "check_statuses": dict(stats.check_statuses),
        "agent": {
            "finish_count": stats.act_finished,
            "interrupted": stats.act_interrupted,
            "rounds_p50": _percentile(stats.act_rounds, 50),
            "rounds_p90": _percentile(stats.act_rounds, 90),
        },
        "gaps": [
            {
                "id": g.id,
                "severity": g.severity,
                "title": g.title,
                "evidence": g.evidence,
                "design_hook": g.design_hook,
            }
            for g in gaps
        ],
    }


def format_report(
    stats: EvalStats, gaps: list[EvalGap], *, flow: "object | None" = None,
) -> str:
    lines = [
        "lmloop eval report (local usage.jsonl)",
        f"  events: {stats.events}  invalid: {stats.invalid_rows}",
    ]
    if stats.first_ts:
        lines.append(f"  range: {stats.first_ts} .. {stats.last_ts}")
    if stats.features:
        top = stats.features.most_common(8)
        lines.append("  features: " + ", ".join(f"{k}={v}" for k, v in top))
    if stats.tools:
        top = stats.tools.most_common(6)
        lines.append("  tools: " + ", ".join(f"{k}={v}" for k, v in top))
    if stats.until_outcomes:
        lines.append("  until: " + ", ".join(
            f"{k}={v}" for k, v in stats.until_outcomes.most_common()
        ))
    lines.append("")
    if not gaps:
        lines.append("  gaps: none flagged at current thresholds")
    else:
        lines.append("  gaps:")
        for g in gaps:
            lines.append(f"    [{g.severity}] {g.id}: {g.title}")
            lines.append(f"           {g.evidence}")
            lines.append(f"           → {g.design_hook}")
    if flow is not None:
        from .workflow import FlowStats, compact_flow_summary
        if isinstance(flow, FlowStats):
            lines.append("")
            lines.append("  flow:")
            for row in compact_flow_summary(flow, {"until_max_steps": 12}).splitlines():
                lines.append(f"    {row}")
    return "\n".join(lines)


def last_report_path() -> Path:
    return USAGE_PATH.parent / "evals" / "last_report.json"


def write_last_report(payload: dict) -> Path:
    path = last_report_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def design_doc_skeleton(gaps: list[EvalGap], *, title: str = "Usage-driven improvement") -> str:
    """Markdown outline an agent or human can paste into a new DESIGN_*.md."""
    lines = [
        f"# Design: {title}",
        "",
        "**Status:** proposed (generated from local usage evals)",
        "**Source:** ``lmloop eval --gaps-json`` / ``~/.lmloop/usage.jsonl``",
        "",
        "## Observed gaps",
        "",
    ]
    if not gaps:
        lines.append("_No gaps flagged; collect more usage or lower thresholds in evals.py._")
    else:
        for g in gaps:
            lines.append(f"### {g.id} ({g.severity})")
            lines.append("")
            lines.append(f"- **Symptom:** {g.title}")
            lines.append(f"- **Evidence:** {g.evidence}")
            lines.append(f"- **Extend:** {g.design_hook}")
            lines.append("")
    lines.extend([
        "## Proposed changes",
        "",
        "| Gap | Smallest shippable fix | Test / eval lock-in |",
        "|---|---|---|",
    ])
    for g in gaps:
        lines.append(
            f"| {g.id} | (fill) | extend ``tests/test_evals.py`` or area test |"
        )
    lines.extend([
        "",
        "## Non-goals",
        "",
        "- Sending usage off-machine",
        "- Auto-editing production code without human review",
        "",
        "## Open questions",
        "",
        "- Which gaps reproduce across projects vs one-off?",
        "",
    ])
    return "\n".join(lines)


def _percentile(values: list[int], pct: int) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, (pct * len(ordered) - 1) // 100))
    return ordered[idx]


_ERROR_PREFIX = re.compile(r"^ERROR:")


def is_tool_error_result(text: str) -> bool:
    return bool(_ERROR_PREFIX.match((text or "").strip()))

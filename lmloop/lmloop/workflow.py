"""Deterministic workflow stats from until/graph logs (leaf module)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import memory
from .commands import FlowRequest
from .config import cfg_int, project_dir


@dataclass
class FlowStats:
    until_runs: int = 0
    until_pass: int = 0
    until_paused: int = 0
    maker_cycles: list = field(default_factory=list)
    eval_blocked: int = 0
    eval_total: int = 0
    check_pass: int = 0
    check_total: int = 0

    def to_dict(self) -> dict:
        cycles = sorted(self.maker_cycles)
        p90 = cycles[int(0.9 * (len(cycles) - 1))] if cycles else 0
        return {
            "until_runs": self.until_runs,
            "until_pass": self.until_pass,
            "until_paused": self.until_paused,
            "maker_cycles_median": cycles[len(cycles) // 2] if cycles else 0,
            "maker_cycles_p90": p90,
            "maker_cycles_max": max(cycles) if cycles else 0,
            "check_pass_rate": (self.check_pass / self.check_total) if self.check_total else None,
            "eval_blocked_rate": (self.eval_blocked / self.eval_total) if self.eval_total else None,
        }


def _load_rows(path: Path) -> list:
    return memory.read_jsonl(path)


def count_terminal_runs(slug: "str | None" = None) -> int:
    """Finished until or graph runs (last row ``done``). Used by ``graph propose``."""
    root = project_dir(slug)
    total = 0
    until_dir = root / "until"
    if until_dir.is_dir():
        for path in sorted(until_dir.glob("*.jsonl")):
            rows = _load_rows(path)
            if rows and rows[-1].get("role") == "done":
                total += 1
    graphs_dir = root / "graphs"
    if graphs_dir.is_dir():
        for path in sorted(graphs_dir.glob("*/*.jsonl")):
            rows = _load_rows(path)
            if rows and rows[-1].get("role") == "done":
                total += 1
    return total


def compact_flow_summary(stats: FlowStats, cfg: dict) -> str:
    """Short factual summary for graph authoring prompts."""
    data = stats.to_dict()
    lines = [
        f"until runs: {stats.until_runs} pass {stats.until_pass} paused {stats.until_paused}",
        f"maker cycles median {data.get('maker_cycles_median')} p90 {data.get('maker_cycles_p90')}",
    ]
    if stats.check_total:
        lines.append(
            f"check pass rate {stats.check_pass}/{stats.check_total}",
        )
    rules = flow_rules(stats, cfg)
    if rules:
        lines.append("flow signals:")
        lines.extend(f"- {r}" for r in rules[:8])
    return "\n".join(lines)


def collect_flow_stats(slug: "str | None" = None) -> FlowStats:
    stats = FlowStats()
    root = project_dir(slug)
    until_dir = root / "until"
    if until_dir.is_dir():
        for path in sorted(until_dir.glob("*.jsonl")):
            rows = _load_rows(path)
            if not rows:
                continue
            stats.until_runs += 1
            makers = sum(1 for r in rows if r.get("role") == "maker")
            if makers:
                stats.maker_cycles.append(makers)
            last = rows[-1]
            if last.get("role") == "done":
                stats.until_pass += 1
            elif last.get("role") == "pause":
                stats.until_paused += 1
            for row in rows:
                if row.get("role") == "check":
                    stats.check_total += 1
                    if row.get("status") == "pass":
                        stats.check_pass += 1
                if row.get("role") == "eval":
                    stats.eval_total += 1
                    if row.get("status") == "blocked":
                        stats.eval_blocked += 1
    return stats


def flow_rules(stats: FlowStats, cfg: dict) -> list[str]:
    out: list[str] = []
    data = stats.to_dict()
    max_steps = cfg_int(cfg, "until_max_steps")
    p90 = data.get("maker_cycles_p90") or 0
    if max_steps and p90 >= int(0.9 * max_steps):
        out.append(
            f"budget-bound: p90 maker cycles {p90} ≥ 0.9×until_max_steps ({max_steps})"
        )
    if stats.eval_total >= 4 and (data.get("eval_blocked_rate") or 0) >= 0.3:
        out.append(
            f"blocked-loop: eval blocked {stats.eval_blocked}/{stats.eval_total} runs"
        )
    if stats.until_runs >= 4 and stats.check_total == 0:
        out.append(
            "checker-only: many runs finish without deterministic checks — document a gate"
        )
    return out


def render_flow(stats: FlowStats, cfg: dict, request: FlowRequest) -> str:
    """Text or JSON body for ``lmloop flow`` and ``/flow``."""
    if request.as_json:
        return json.dumps(stats.to_dict(), indent=2)
    return format_flow_report(stats, cfg)


def company_prefix_note(slug: "str | None" = None) -> str:
    """One line when company runs recorded a stable or drifted prompt prefix."""
    root = memory.project_dir(slug) / "company"
    if not root.is_dir():
        return ""
    by_node: dict[str, set] = {}
    for path in sorted(root.glob("*/run.jsonl")):
        for row in memory.read_jsonl(path):
            if row.get("role") != "prefix" or not row.get("node"):
                continue
            by_node.setdefault(str(row["node"]), set()).add(str(row.get("prefix_hash") or ""))
    if not by_node:
        return ""
    drifted = [name for name, hashes in by_node.items() if len(hashes) > 1]
    if drifted:
        return "company prefix drifted: " + ", ".join(sorted(drifted))
    return f"company prefix stable across {len(by_node)} node(s)"


def format_flow_report(stats: FlowStats, cfg: dict) -> str:
    lines = ["lmloop flow · project stats", json.dumps(stats.to_dict(), indent=2)]
    prefix = company_prefix_note()
    if prefix:
        lines.append(prefix)
    rules = flow_rules(stats, cfg)
    if rules:
        lines.append("")
        lines.append("Suggestions:")
        lines.extend(f"  · {r}" for r in rules)
    else:
        lines.append("")
        lines.append("Suggestions: (none)")
    return "\n".join(lines)

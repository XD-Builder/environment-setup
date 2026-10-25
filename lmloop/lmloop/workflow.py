"""Deterministic workflow stats from until/graph logs (leaf module)."""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from . import graph_dsl, memory
from .core.commands import FlowRequest
from .core.config import Config, cfg_int, project_dir

_DROP_PREFIX = "dropped unrunnable check: "


@dataclass
class FlowStats:
    until_runs: int = 0
    until_pass: int = 0
    until_paused: int = 0
    until_gate_no: int = 0
    terminal_runs: int = 0
    checker_only_runs: int = 0
    maker_cycles: list = field(default_factory=list)
    eval_blocked: int = 0
    eval_total: int = 0
    check_pass: int = 0
    check_total: int = 0
    check_observations: list = field(default_factory=list)
    denial_shapes: dict = field(default_factory=dict)
    dropped_sources: dict = field(default_factory=dict)
    transitions: list = field(default_factory=list)
    baseline_fail_nodes: set = field(default_factory=set)
    dead_nodes: list = field(default_factory=list)
    tokens_by_node: dict = field(default_factory=dict)
    graph_entered: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        cycles = sorted(self.maker_cycles)
        p90 = cycles[int(0.9 * (len(cycles) - 1))] if cycles else 0
        data = {
            "until_runs": self.until_runs,
            "until_pass": self.until_pass,
            "until_paused": self.until_paused,
            "until_gate_no": self.until_gate_no,
            "terminal_runs": self.terminal_runs,
            "checker_only_runs": self.checker_only_runs,
            "maker_cycles_median": cycles[len(cycles) // 2] if cycles else 0,
            "maker_cycles_p90": p90,
            "maker_cycles_max": max(cycles) if cycles else 0,
            "check_pass_rate": (self.check_pass / self.check_total) if self.check_total else None,
            "eval_blocked_rate": (self.eval_blocked / self.eval_total) if self.eval_total else None,
        }
        if self.tokens_by_node:
            data["tokens_by_node"] = dict(sorted(self.tokens_by_node.items()))
        return data


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


def compact_flow_summary(stats: FlowStats, cfg: Config) -> str:
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


def _denial_shape(cmd: str) -> str:
    try:
        parts = shlex.split(cmd)
    except ValueError:
        parts = cmd.split()
    return parts[0] if parts else cmd


def _note_denial(stats: FlowStats, run_id: str, commands) -> None:
    for cmd in commands or []:
        shape = _denial_shape(str(cmd))
        if not shape:
            continue
        stats.denial_shapes.setdefault(shape, set()).add(run_id)


def _note_dropped(stats: FlowStats, run_id: str, row: dict) -> None:
    dropped = list(row.get("dropped") or [])
    if not dropped:
        for line in str(row.get("handoff") or "").splitlines():
            if line.startswith(_DROP_PREFIX):
                dropped.append({"cmd": line[len(_DROP_PREFIX):], "source": ""})
    checks = {
        item.get("cmd"): item.get("source") or ""
        for item in (row.get("checks") or [])
        if isinstance(item, dict)
    }
    for item in dropped:
        if not isinstance(item, dict):
            continue
        source = item.get("source") or checks.get(item.get("cmd")) or ""
        if source:
            stats.dropped_sources.setdefault(source, set()).add(run_id)


def _baseline_failed(path: Path) -> bool:
    if not path.is_file():
        return False
    for row in _load_rows(path):
        if row.get("role") != "baseline":
            continue
        for result in row.get("results") or []:
            if isinstance(result, dict) and result.get("status") == "fail":
                return True
    return False


def _scan_until(stats: FlowStats, path: Path, rows: list) -> None:
    run_id = str(path)
    stats.until_runs += 1
    makers = sum(1 for r in rows if r.get("role") == "maker")
    if makers:
        stats.maker_cycles.append(makers)
    last = rows[-1]
    if last.get("role") == "done":
        stats.until_pass += 1
    elif last.get("role") == "pause":
        stats.until_paused += 1
    elif last.get("role") == "gate" and last.get("status") == "no":
        stats.until_gate_no += 1
    finished = last.get("role") in ("done", "mine")
    saw_check_pass = False
    saw_eval_pass = False
    for row in rows:
        role = row.get("role")
        if role == "check":
            stats.check_total += 1
            if row.get("status") == "pass":
                stats.check_pass += 1
                saw_check_pass = True
            tree = str(row.get("tree") or "")
            for result in row.get("results") or []:
                if not isinstance(result, dict):
                    continue
                stats.check_observations.append({
                    "cmd": result.get("cmd") or "",
                    "status": result.get("status") or "",
                    "tree": tree,
                })
        elif role == "eval":
            stats.eval_total += 1
            if row.get("status") == "blocked":
                stats.eval_blocked += 1
            if row.get("status") == "pass":
                saw_eval_pass = True
        elif role == "maker":
            _note_denial(stats, run_id, row.get("denied"))
        elif role == "baseline":
            _note_dropped(stats, run_id, row)
    if finished:
        stats.terminal_runs += 1
        if saw_eval_pass and not saw_check_pass:
            stats.checker_only_runs += 1


def _scan_graph(stats: FlowStats, path: Path, rows: list) -> None:
    name = path.parent.name
    if rows and rows[0].get("role") == "meta" and rows[0].get("name"):
        name = str(rows[0]["name"])
    bucket = stats.graph_entered.setdefault(name, {"runs": 0, "nodes": set()})
    bucket["runs"] += 1
    prev: "tuple[str, str] | None" = None
    for row in rows:
        if row.get("role") != "node":
            continue
        node = str(row.get("node") or "")
        status = str(row.get("status") or "")
        if node:
            bucket["nodes"].add(node)
        if prev and prev[0] and node and prev[1] in ("pass", "fail", "blocked"):
            stats.transitions.append((prev[0], prev[1], node))
        prev = (node, status)
        if node and not row.get("until_run"):
            _note_denial(stats, str(path), row.get("denied"))
        tokens = int((row.get("usage") or {}).get("total_tokens") or 0)
        if tokens and node:
            stats.tokens_by_node[node] = stats.tokens_by_node.get(node, 0) + tokens
        until_raw = row.get("until_run") or ""
        if node and until_raw and _baseline_failed(Path(until_raw)):
            stats.baseline_fail_nodes.add(node)


def _authored_node_names(name: str) -> "set[str] | None":
    try:
        defn = graph_dsl.load_graph(name)
    except graph_dsl.GraphError:
        return None
    return {node.name for node in defn.nodes}


def _fill_dead_nodes(stats: FlowStats) -> None:
    dead: list[str] = []
    for name, bucket in sorted(stats.graph_entered.items()):
        if bucket["runs"] < 5:
            continue
        authored = _authored_node_names(name)
        if not authored:
            continue
        missing = sorted(authored - bucket["nodes"])
        dead.extend(f"{name}:{node}" for node in missing)
    stats.dead_nodes = dead


def collect_flow_stats(slug: "str | None" = None) -> FlowStats:
    stats = FlowStats()
    root = project_dir(slug)
    until_dir = root / "until"
    if until_dir.is_dir():
        for path in sorted(until_dir.glob("*.jsonl")):
            rows = _load_rows(path)
            if rows:
                _scan_until(stats, path, rows)
    graphs_dir = root / "graphs"
    if graphs_dir.is_dir():
        for path in sorted(graphs_dir.glob("*/*.jsonl")):
            rows = _load_rows(path)
            if rows:
                _scan_graph(stats, path, rows)
    _fill_dead_nodes(stats)
    return stats


def _rule_budget(stats: FlowStats, cfg: Config) -> "str | None":
    max_steps = cfg_int(cfg, "until_max_steps")
    p90 = stats.to_dict().get("maker_cycles_p90") or 0
    if not max_steps or p90 < int(0.9 * max_steps):
        return None
    if stats.until_runs >= 4 and stats.until_paused * 2 < stats.until_runs:
        return None
    return f"p90 maker cycles {p90} ≥ 0.9×until_max_steps ({max_steps})"


def _rule_checker(stats: FlowStats, _cfg: Config) -> "str | None":
    if stats.terminal_runs < 4:
        return None
    if stats.checker_only_runs * 2 < stats.terminal_runs:
        return None
    return (
        f"{stats.checker_only_runs}/{stats.terminal_runs} finished runs "
        "ended on eval with no proving check"
    )


def _rule_flaky(stats: FlowStats, _cfg: Config) -> "str | None":
    groups: dict[tuple[str, str], set] = {}
    for obs in stats.check_observations:
        tree = obs.get("tree") or ""
        cmd = obs.get("cmd") or ""
        if not tree or not cmd:
            continue
        groups.setdefault((cmd, tree), set()).add(obs.get("status") or "")
    for (cmd, tree), statuses in groups.items():
        statuses.discard("")
        if len(statuses) >= 2:
            shown = ", ".join(sorted(statuses))
            return f"{cmd} changed result on tree {tree[:12]} ({shown})"
    return None


def _rule_blocked(stats: FlowStats, _cfg: Config) -> "str | None":
    if stats.eval_total < 4:
        return None
    rate = stats.eval_blocked / stats.eval_total
    if rate < 0.3:
        return None
    return f"eval blocked {stats.eval_blocked}/{stats.eval_total} runs"


def _rule_denial(stats: FlowStats, _cfg: Config) -> "str | None":
    hits = [
        (shape, len(runs))
        for shape, runs in stats.denial_shapes.items()
        if len(runs) >= 3
    ]
    if not hits:
        return None
    shape, count = max(hits, key=lambda item: (item[1], item[0]))
    return f"{shape} denied in {count} runs"


def _rule_dead(stats: FlowStats, _cfg: Config) -> "str | None":
    if not stats.dead_nodes:
        return None
    shown = ", ".join(stats.dead_nodes[:8])
    return f"never entered in ≥5 runs: {shown}"


def _rule_hot(stats: FlowStats, _cfg: Config) -> "str | None":
    if len(stats.transitions) < 4:
        return None
    counts: dict[tuple[str, str], int] = {}
    for src, on, dst in stats.transitions:
        if on != "fail" or src in stats.baseline_fail_nodes:
            continue
        counts[(src, dst)] = counts.get((src, dst), 0) + 1
    if not counts:
        return None
    (src, dst), count = max(counts.items(), key=lambda item: (item[1], item[0]))
    total = len(stats.transitions)
    if count * 2 < total:
        return None
    return f"{src} -> {dst} on fail: {count}/{total} transitions"


def _rule_source(stats: FlowStats, _cfg: Config) -> "str | None":
    hits = [
        (source, len(runs))
        for source, runs in stats.dropped_sources.items()
        if len(runs) >= 3
    ]
    if not hits:
        return None
    source, count = max(hits, key=lambda item: (item[1], item[0]))
    return f"{source} dropped at baseline in {count} runs"


FLOW_RULES = (
    ("budget-bound", _rule_budget),
    ("checker-only", _rule_checker),
    ("flaky-gate", _rule_flaky),
    ("blocked-loop", _rule_blocked),
    ("repeat-denial", _rule_denial),
    ("dead-node", _rule_dead),
    ("hot-cycle", _rule_hot),
    ("bad-source", _rule_source),
)


def flow_rules(stats: FlowStats, cfg: Config) -> list[str]:
    out: list[str] = []
    for rule_id, fn in FLOW_RULES:
        evidence = fn(stats, cfg)
        if evidence:
            out.append(f"{rule_id}: {evidence}")
    return out


def render_flow(stats: FlowStats, cfg: Config, request: FlowRequest) -> str:
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


def format_flow_report(stats: FlowStats, cfg: Config) -> str:
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

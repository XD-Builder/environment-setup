"""Compact until-run state and artifact index (Meta-style L2).

Sidecar layout: ``until/<ts>.meta/`` next to the flat ``until/<ts>.jsonl`` log.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .config import utc_now


def _clip(text: str, limit: int) -> str:
    body = text or ""
    if len(body) <= limit:
        return body
    return body[:limit] + "…"

RUN_STATE_MAX = 4000
ARTIFACT_SUMMARY_MAX = 800
_CONTROL_STEER = (
    "You may use remaining budget. Prefer verifying failures before large "
    "rewrites. Cite check output and files changed."
)

_ARTIFACT_ID_RE = re.compile(r"^a(\d+)$", re.I)


def normalize_until_control(value: str) -> str:
    """Return a valid until_control mode or ``off``."""
    low = (value or "off").strip().lower()
    if low in ("off", "budget", "meta"):
        return low
    return "off"


def format_budget_block(
    *,
    run_tokens: int,
    token_budget: int,
    makers_this_call: int,
    until_max_steps: int,
    maker_count: int,
) -> str:
    """Human-readable budget footer for maker/eval prompts."""
    limit = "unlimited" if token_budget <= 0 else str(token_budget)
    return (
        f"- Tokens: {run_tokens} / {limit}\n"
        f"- Maker cycles this invocation: {makers_this_call} / {until_max_steps}\n"
        f"- Maker cycles recorded in log: {maker_count}"
    )


class UntilArtifactStore:
    """Append-only artifact index and rewritten ``state.md`` for one until run."""

    def __init__(self, run_path: Path) -> None:
        self.run_path = Path(run_path)
        self.meta_dir = self.run_path.parent / f"{self.run_path.stem}.meta"

    def ensure(self) -> None:
        self.meta_dir.mkdir(parents=True, exist_ok=True)

    @property
    def artifacts_path(self) -> Path:
        return self.meta_dir / "artifacts.jsonl"

    @property
    def state_path(self) -> Path:
        return self.meta_dir / "state.md"

    def load_artifacts(self) -> list[dict]:
        path = self.artifacts_path
        if not path.exists():
            return []
        rows: list[dict] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows

    def _next_id(self) -> str:
        best = 0
        for row in self.load_artifacts():
            match = _ARTIFACT_ID_RE.match(str(row.get("id") or ""))
            if match:
                best = max(best, int(match.group(1)))
        return f"a{best + 1}"

    def append_artifact(
        self,
        *,
        kind: str,
        step: int,
        summary: str,
        refs: "list[str] | None" = None,
        pointers: "dict | None" = None,
    ) -> str:
        """Append one artifact row; return its id."""
        self.ensure()
        body = (summary or "").strip()
        if len(body) > ARTIFACT_SUMMARY_MAX:
            body = body[:ARTIFACT_SUMMARY_MAX] + "…"
        prior = self.load_artifacts()
        prev_ids = [str(row["id"]) for row in prior if row.get("id")]
        if refs is None:
            link = [prev_ids[-1]] if prev_ids else []
        else:
            link = list(refs)
        row = {
            "id": self._next_id(),
            "ts": utc_now(),
            "kind": kind,
            "step": step,
            "summary": body,
            "refs": link,
            "pointers": dict(pointers or {}),
        }
        with self.artifacts_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        return str(row["id"])

    def write_state(self, text: str) -> None:
        self.ensure()
        body = (text or "").strip()
        if len(body) > RUN_STATE_MAX:
            body = body[:RUN_STATE_MAX] + "\n… [truncated]"
        self.state_path.write_text(body + "\n", encoding="utf-8")

    def read_state(self) -> str:
        if not self.state_path.exists():
            return ""
        return self.state_path.read_text(encoding="utf-8").strip()

    def assignment_from_state(self) -> str:
        """Parse **Next focus** from ``state.md``, if present."""
        state = self.read_state()
        if not state:
            return ""
        lines = state.splitlines()
        for idx, line in enumerate(lines):
            if line.strip().lower() == "## next focus" and idx + 1 < len(lines):
                return lines[idx + 1].strip()
        return ""


def _status_rows_from_check(results: list) -> list[tuple[str, str, str]]:
    """Return (subgoal, state, evidence) tuples from check results."""
    rows: list[tuple[str, str, str]] = []
    for row in results or []:
        cmd = str(row.get("cmd") or "").strip()
        if not cmd:
            continue
        status = str(row.get("status") or "fail")
        if status == "pass":
            state = "done"
        elif status == "blocked":
            state = "unverified"
        else:
            state = "failing"
        snippet = _clip(str(row.get("output") or ""), 120)
        evidence = cmd if not snippet else f"{cmd} — {snippet.splitlines()[0]}"
        rows.append((cmd, state, evidence))
    return rows


def host_assess_state(
    store: UntilArtifactStore,
    *,
    goal: str,
    run_tokens: int,
    token_budget: int,
    makers_this_call: int,
    until_max_steps: int,
    maker_count: int,
    check_results: "list | None",
    eval_status: str = "",
    eval_handoff: str = "",
    changed_files: "list[str] | None",
    failing_cmd: str = "",
) -> str:
    """Rewrite ``state.md`` after a failed check or eval. Returns next focus."""
    budget = format_budget_block(
        run_tokens=run_tokens,
        token_budget=token_budget,
        makers_this_call=makers_this_call,
        until_max_steps=until_max_steps,
        maker_count=maker_count,
    )
    status_rows = _status_rows_from_check(check_results or [])
    if not status_rows and eval_status == "fail":
        status_rows = [
            ("goal verification", "failing", _clip(eval_handoff, 160)),
        ]
    risks: list[str] = []
    if changed_files:
        risks.append(f"Recent files touched: {', '.join(changed_files[:12])}")
    if any(st == "unverified" for _, st, _ in status_rows):
        risks.append("A check was blocked — confirm tooling or gates.")
    next_focus = ""
    for cmd, state, _ev in status_rows:
        if state == "failing":
            next_focus = f"Fix failing check: {cmd}"
            break
    if not next_focus and failing_cmd:
        next_focus = f"Fix failing check: {failing_cmd}"
    if not next_focus and eval_status == "fail":
        next_focus = "Address eval feedback and re-run checks."
    if not next_focus:
        next_focus = "Make the next checkable increment toward the goal."

    lines = [
        "## Goal",
        goal.strip() or "(none)",
        "",
        "## Budget",
        budget,
        "",
        "## Status map",
        "| subgoal | state | evidence |",
        "| --- | --- | --- |",
    ]
    for subgoal, state, evidence in status_rows:
        lines.append(f"| {subgoal} | {state} | {evidence} |")
    if not status_rows:
        lines.append("| (none yet) | unknown | run checks |")
    lines.extend(["", "## Open risks"])
    if risks:
        lines.extend(f"- {r}" for r in risks)
    else:
        lines.append("- (none flagged)")
    lines.extend(["", "## Next focus", next_focus])
    store.write_state("\n".join(lines))
    return next_focus


def control_steering(control_mode: str) -> str:
    if normalize_until_control(control_mode) == "off":
        return ""
    return _CONTROL_STEER

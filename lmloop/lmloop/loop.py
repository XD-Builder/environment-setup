"""Goal loop: isolated maker act() until an external check or eval says done.

A loop is a while-statement. This module owns the stop condition — exit code or
an isolated checker thread — so the maker cannot grade its own work.
"""

import json
import shlex
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import agent, memory, server, skills, snapshot, status as status_mod, tools
from .checks import (
    PlannedCheck,
    CheckPlan,
    apply_baseline,
    confirm_plan,
    flags_plan,
    format_plan,
    infer_plan,
    judge_cycle,
    order_cycle_checks,
    parse_proposed_commands,
    plan_needs_proposal,
    PROPOSE_PROMPT,
    shell_evidence_table,
)
from .config import DEFAULTS, cfg_bool, cfg_int, cfg_str, project_dir, utc_now
from .server import eval_model_name
from .tools import run_shell, shell_confirm_flags

STATUS_LINE_PREFIX = "STATUS:"
EXIT_CODE_PREFIX = "[exit code:"
VALID_EVAL_STATUS = frozenset({"pass", "fail", "blocked"})
META_ROLE = "meta"
PAUSE_ROLE = "pause"
# User approved this cycle's denied irreversible actions; handoff holds them as JSON.
APPROVE_ROLE = "approve"
DONE_ROLES = frozenset({"mine", "done"})


def _until_outcome_label(events: list) -> str:
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
CHECK_OUTPUT_LIMIT = 2000
_CHECK_TRUNCATED = "\n... [truncated, {total} chars total]"

MAKER_PROMPT = """You are working toward this goal until an external checker says it is done.
You do not get to declare the overall goal complete.

Goal:
{goal}

Prior handoff (empty on the first cycle):
{handoff}

Do the next checkable increment. Use tools. When you stop, summarize what
changed and what remains. Cite files, commands, or URLs. Do not end with a
STATUS line — a separate checker decides pass/fail.
"""

EVAL_PROMPT = """You are an independent checker. You did not do this work.
Decide whether the user's goal is met. Use tools only to verify evidence
(read files, run tests). Do not continue implementing.

Goal:
{goal}

Maker handoff:
{handoff}

Check command output (empty if none):
{check_output}

Shell evidence (recent tool exits, not a verdict):
{shell_evidence}

End your reply with exactly one last line, one of:
STATUS: pass
STATUS: fail
STATUS: blocked

pass — the goal is met with cited evidence.
fail — more work is needed.
blocked — you cannot tell, or a human must decide.
"""


def clip_check_output(output: str, limit: int = CHECK_OUTPUT_LIMIT) -> str:
    """Bound check-command text for display/handoff. Never silent: mark the cut."""
    body = output or ""
    if len(body) <= limit:
        return body
    return body[:limit] + _CHECK_TRUNCATED.format(total=len(body))


def last_assistant(messages: list) -> str:
    """Last non-empty assistant reply in a message thread."""
    for m in reversed(messages or []):
        if m.get("role") == "assistant" and (m.get("content") or "").strip():
            return (m.get("content") or "").strip()
    return ""


def parse_eval_status(text: str) -> str:
    """Read STATUS: pass|fail|blocked from the last matching line. Else blocked."""
    for line in reversed((text or "").strip().splitlines()):
        s = line.strip()
        if not s.upper().startswith(STATUS_LINE_PREFIX):
            continue
        token = s.split(":", 1)[1].strip().split()
        word = token[0].lower() if token else ""
        if word in VALID_EVAL_STATUS:
            return word
        return "blocked"
    return "blocked"


def check_status_from_output(output: str) -> str:
    """Map run_shell text to pass/fail/blocked. Never calls the model."""
    body = (output or "").strip()
    if body.startswith("DENIED:"):
        return "blocked"
    if body.startswith("ERROR:"):
        return "blocked"
    last = body.splitlines()[-1] if body else ""
    if last.startswith(EXIT_CODE_PREFIX):
        rest = last[len(EXIT_CODE_PREFIX):].strip().rstrip("]")
        try:
            code = int(rest)
        except ValueError:
            return "fail"
        if code in (126, 127):
            return "blocked"
        return "pass" if code == 0 else "fail"
    return "fail"


_UNTIL_USAGE = "usage: until [--check <cmd>] [--keep <cmd>] <goal>"


@dataclass(frozen=True)
class UntilArgs:
    """Parsed ``until`` invocation. Empty checks and keeps means infer a plan."""

    goal: str = ""
    checks: tuple = ()
    keeps: tuple = ()
    err: "str | None" = None

    @property
    def check_cmd(self) -> "str | None":
        return self.checks[0] if self.checks else None


def split_check_flags(words: list) -> "tuple[tuple, tuple, list, str | None]":
    """Pull repeatable ``--check`` / ``--keep`` off ``words``. Fail closed."""
    checks: list[str] = []
    keeps: list[str] = []
    rest: list[str] = []
    i = 0
    while i < len(words):
        word = words[i]
        if word in ("--check", "--keep"):
            if i + 1 >= len(words) or str(words[i + 1]).startswith("--"):
                return (), (), [], _UNTIL_USAGE
            cmd = str(words[i + 1]).strip()
            if not cmd:
                return (), (), [], _UNTIL_USAGE
            (checks if word == "--check" else keeps).append(cmd)
            i += 2
            continue
        rest.append(word)
        i += 1
    return tuple(checks), tuple(keeps), rest, None


def parse_until_args(words: list) -> UntilArgs:
    """Parse an until goal. ``err`` is set when the line is invalid."""
    checks, keeps, rest, err = split_check_flags(words)
    if err:
        return UntilArgs(err=err)
    goal = " ".join(rest).strip()
    if not goal:
        return UntilArgs(checks=checks, keeps=keeps, err=_UNTIL_USAGE)
    return UntilArgs(goal=goal, checks=checks, keeps=keeps)


def parse_until_arg_line(arg: str) -> UntilArgs:
    """Parse a REPL /until argument line with shlex (quoted --check)."""
    try:
        words = shlex.split(arg or "")
    except ValueError as e:
        return UntilArgs(err=str(e))
    return parse_until_args(words)


def eval_max_rounds(cfg: dict) -> int:
    """Gather-round budget for an eval ``act()``. Maker keeps ``max_rounds``."""
    return max(1, cfg_int(cfg, "eval_max_rounds"))


def isolated_act(
    cfg: dict, model: str, user_text: str, *,
    confirm_gate=None, echo=print, echo_status=None, echo_error=None,
    echo_tool=None, echo_round=None, context_limit: int = 0,
    context_reserve: int = DEFAULTS["context_reserve"], workspace_root: "Path | None" = None,
    log_label: str = "",
    readonly: bool = False,
    no_tools: bool = False,
    max_rounds: "int | None" = None,
    clock_now=None,
    extra_readable: "list | None" = None,
    memory_block: "str | None" = None,
    usage_stats: "dict | None" = None,
    model_override: "str | None" = None,
) -> "tuple[list, Path] | None":
    """Run act() on a fresh thread. Returns (messages, session_log), or None.

    KeyboardInterrupt is logged, then re-raised so ``run_until`` can pause.
    ``clock_now`` freezes the system-prompt Clock across maker/eval cycles.
    """
    if echo_status is None:
        echo_status = echo
    if echo_error is None:
        echo_error = echo_status
    use_model = model_override or model
    stats = usage_stats if usage_stats is not None else {}
    messages = [{"role": "system", "content": skills.system_prompt(
        cfg, workspace_root, clock_now=clock_now, memory_block=memory_block,
    )}]
    session_log = memory.new_session_log()
    if log_label:
        memory.log_event(session_log, "user", log_label)
    messages.append({"role": "user", "content": user_text})
    try:
        agent.act(
            cfg, use_model, messages, session_log=session_log,
            confirm_gate=confirm_gate,
            echo=echo,
            echo_status=echo_status,
            echo_tool=echo_tool,
            echo_round=echo_round,
            context_limit=context_limit,
            context_reserve=context_reserve,
            workspace_root=workspace_root,
            readonly=readonly,
            no_tools=no_tools,
            max_rounds=max_rounds,
            extra_readable=extra_readable,
            stats=stats,
        )
    except server.ServerError as e:
        memory.log_event(session_log, "system", f"error: {e}")
        echo_error(f"error: {e}")
        return None
    except KeyboardInterrupt:
        memory.log_event(session_log, "system", "interrupted")
        echo_status(
            f"\n[interrupted — live conversation unchanged; {status_mod.MSG_RESUME}]"
        )
        raise
    return messages, session_log


def until_dir(slug: "str | None" = None) -> Path:
    d = project_dir(slug) / "until"
    d.mkdir(parents=True, exist_ok=True)
    return d


def all_until_runs(slug: "str | None" = None) -> "list[Path]":
    d = project_dir(slug) / "until"
    if not d.exists():
        return []
    return sorted(d.glob("*.jsonl"))


@dataclass
class UntilRun:
    """Append-only until-run log. Current step is computed from events."""

    path: Path
    goal: str
    check_cmd: "str | None" = None
    events: list = field(default_factory=list)

    @classmethod
    def create(cls, goal: str, check_cmd: "str | None" = None,
               slug: "str | None" = None, checks: "tuple | None" = None,
               keeps: "tuple | None" = None) -> "UntilRun":
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        d = until_dir(slug)
        path = d / f"{ts}.jsonl"
        n = 1
        while path.exists():
            path = d / f"{ts}-{n}.jsonl"
            n += 1
        check_list = list(checks or ())
        if check_cmd and check_cmd not in check_list:
            check_list.insert(0, check_cmd)
        planned = flags_plan(tuple(check_list), tuple(keeps or ()))
        first = next((item.cmd for item in planned if item.role == "check"), "")
        run = cls(path=path, goal=goal, check_cmd=first or None, events=[])
        run._write({
            "ts": utc_now(),
            "role": META_ROLE,
            "goal": goal,
            "check_cmd": first,
            "checks": [item.as_dict() for item in planned],
            "explicit": bool(planned),
        })
        return run

    @classmethod
    def load(cls, path: Path) -> "UntilRun":
        events = memory.read_jsonl(path)
        goal = ""
        check_cmd = None
        if events and events[0].get("role") == META_ROLE:
            goal = events[0].get("goal") or ""
            check_cmd = events[0].get("check_cmd") or None
        return cls(path=path, goal=goal, check_cmd=check_cmd, events=events)

    def _write(self, row: dict) -> None:
        memory.append_jsonl(self.path, row)
        self.events.append(row)

    def append(self, role: str, status: str, handoff: str = "",
               session: str = "", checks=None, results=None,
               snapshot_ref: str = "", **extra: object) -> None:
        step = sum(1 for e in self.events if e.get("role") not in (META_ROLE,))
        row = {
            "ts": utc_now(),
            "step": step,
            "role": role,
            "status": status,
            "handoff": handoff,
            "session": session,
        }
        if checks is not None:
            row["checks"] = checks
        if results is not None:
            row["results"] = results
        if snapshot_ref:
            row["snapshot_ref"] = snapshot_ref
        for key, val in extra.items():
            if val is not None and val != "" and val != ():
                row[key] = val
        self._write(row)

    def current_checks(self) -> list:
        """Latest plan. A stored empty list means the user skipped inference."""
        latest = None
        for ev in self.events:
            if "checks" in ev:
                latest = ev["checks"]
        if latest is not None:
            return [PlannedCheck.from_dict(row) for row in latest if row.get("cmd")]
        if self.check_cmd:
            return [PlannedCheck(
                cmd=self.check_cmd, role="check", source="flag",
                tier="authoritative", reason="--check", user_typed=True,
            )]
        return []

    def baseline_statuses(self) -> dict:
        for ev in reversed(self.events):
            if ev.get("role") != "baseline":
                continue
            return {
                row.get("cmd"): row.get("status")
                for row in ev.get("results") or []
                if row.get("cmd")
            }
        return {}

    def explicit_plan(self) -> bool:
        if self.events and self.events[0].get("role") == META_ROLE:
            return bool(self.events[0].get("explicit"))
        return bool(self.check_cmd)

    def last_work(self) -> "dict | None":
        for ev in reversed(self.events):
            if ev.get("role") not in (META_ROLE, PAUSE_ROLE):
                return ev
        return None

    def is_paused(self) -> bool:
        if not self.events:
            return False
        return self.events[-1].get("role") == PAUSE_ROLE

    def is_done(self) -> bool:
        last = self.last_work()
        if last is None:
            return False
        if last.get("role") in DONE_ROLES:
            return True
        if last.get("role") == "abstain":
            return True
        return last.get("role") == "gate" and last.get("status") == "no"

    def last_handoff(self) -> str:
        """Last maker or eval summary. Skips check/gate/pause rows."""
        for ev in reversed(self.events):
            if ev.get("role") in ("maker", "eval"):
                text = ev.get("handoff") or ""
                if text:
                    return text
        return ""

    def last_maker_handoff(self) -> str:
        for ev in reversed(self.events):
            if ev.get("role") == "maker":
                return ev.get("handoff") or ""
        return ""

    def approved_commands(self) -> "list[str]":
        """Commands approved at the last cycle boundary, if that is the last work row."""
        last = self.last_work()
        if not last or last.get("role") != APPROVE_ROLE:
            return []
        try:
            rows = json.loads(last.get("handoff") or "[]")
        except ValueError:
            return []
        return [str(c) for c in rows if isinstance(c, str) and c]

    def session_paths(self) -> "list[Path]":
        seen: list[Path] = []
        have: set[str] = set()
        for ev in self.events:
            raw = ev.get("session") or ""
            if not raw or raw in have:
                continue
            have.add(raw)
            seen.append(Path(raw))
        return seen

    def next_role(self, *, do_mine: bool) -> "str | None":
        """Role to run next, or None when the run is finished."""
        last = self.last_work()
        if last is None:
            return "maker"
        role, status = last.get("role"), last.get("status")
        key = (role, status)
        after_pass = "mine" if do_mine else None
        table = {
            ("maker", "next"): "check" if self.current_checks() else "eval",
            ("check", "pass"): after_pass,
            ("check", "pending"): "eval",
            ("check", "fail"): "maker",
            ("check", "blocked"): "gate",
            ("baseline", "blocked"): "gate",
            ("abstain", "infeasible"): None,
            ("eval", "pass"): after_pass,
            ("eval", "fail"): "maker",
            ("eval", "blocked"): "gate",
            ("gate", "yes"): "maker",
            ("gate", "no"): None,
            (APPROVE_ROLE, "yes"): "maker",
            ("mine", "next"): None,
            ("done", "pass"): None,
        }
        if key in table:
            return table[key]
        return "maker"


def latest_open_until_run(slug: "str | None" = None) -> "UntilRun | None":
    """Most recent until-run that is not done (paused or in progress)."""
    for path in reversed(all_until_runs(slug=slug)):
        run = UntilRun.load(path)
        if run.goal and not run.is_done():
            return run
    return None


def superseded_until_hint(slug: "str | None" = None) -> "str | None":
    """Hint when starting a new until-run would leave an older one open."""
    prior = latest_open_until_run(slug)
    if prior is None:
        return None
    return (
        f"[until · previous run {prior.path.name} still open; "
        "resume uses the latest run]"
    )


def abstain_reason(outcome) -> "str | None":
    """Structural infeasibility that must not start a maker step.

    A missing typed binary still goes to the human gate (it may be fixable).
    A keep that is already failing cannot be repaired by more edits of the goal.
    """
    if getattr(outcome, "status", "") != "blocked":
        return None
    text = " ".join(getattr(outcome, "notes", ()) or ())
    if "invariant already broken" in text:
        return "keep-prebroken"
    return None


def run_check(cfg: dict, command: str, confirm_gate, workspace_root: Path) -> str:
    """Run an until/graph ``--check`` command and return the tool output."""
    destructive, syntax = shell_confirm_flags(cfg)
    return run_shell(
        command,
        confirm_gate=confirm_gate,
        timeout_s=cfg_int(cfg, "shell_timeout_s"),
        confirm_destructive=destructive,
        confirm_shell_syntax=syntax,
        workspace_root=workspace_root,
    )


def run_plan_commands(cfg: dict, commands, confirm_gate, workspace_root: Path) -> list:
    """Run each planned command. One result dict per command."""
    results = []
    for cmd in commands:
        output = run_check(cfg, cmd, confirm_gate, workspace_root)
        results.append({
            "cmd": cmd,
            "status": check_status_from_output(output),
            "output": clip_check_output(output),
        })
    drop_denied(confirm_gate)
    return results


def _plan_handoff(results: list) -> str:
    parts = [f"$ {row['cmd']}\n{row['output']}" for row in results]
    return clip_check_output("\n".join(parts))


def _choice_on(cfg: dict, key: str) -> bool:
    return cfg_str(cfg, key).strip().lower() != "off"


def _read_plan_line(prompt: str = "") -> str:
    try:
        return input(prompt)
    except EOFError:
        print()
        return ""


def _model_propose_checks(
    cfg: dict, model: str, goal: str, root: Path, echo_status,
    *, memory_block: "str | None", clock_now,
) -> tuple:
    """One no-tools call; returns up to two advisory PlannedChecks (V8)."""
    prompt = PROPOSE_PROMPT.format(goal=goal)
    stats: dict = {}
    result = isolated_act(
        cfg, model, prompt,
        confirm_gate=None,
        echo=echo_status,
        echo_status=echo_status,
        workspace_root=root,
        log_label="/until propose-checks",
        no_tools=True,
        max_rounds=1,
        clock_now=clock_now,
        memory_block=memory_block,
        usage_stats=stats,
        model_override=eval_model_name(cfg, model),
    )
    if result is None:
        return ()
    messages, _session = result
    return parse_proposed_commands(last_assistant(messages))


def _maybe_propose_checks(
    planned: list, cfg: dict, model: str, goal: str, root: Path,
    echo_status, *, memory_block, clock_now,
) -> list:
    if not _choice_on(cfg, "check_inference"):
        return planned
    if not plan_needs_proposal(tuple(planned)):
        return planned
    proposed = _model_propose_checks(
        cfg, model, goal, root, echo_status,
        memory_block=memory_block, clock_now=clock_now,
    )
    if not proposed:
        return planned
    for item in proposed:
        echo_status(
            f"  advisory  {item.cmd}\n            {item.reason}",
        )
    return planned + list(proposed)


def _prepare_plan(run: UntilRun, cfg: dict, root: Path, echo_status,
                  interactive: bool, confirm_gate, *, model: str,
                  memory_block: "str | None" = None,
                  clock_now=None) -> None:
    """Infer or reuse a plan, then baseline it once. Resume does not repeat."""
    if any(ev.get("role") in ("plan", "baseline") for ev in run.events):
        return
    if any(ev.get("role") != META_ROLE for ev in run.events):
        return
    if run.explicit_plan() or not _choice_on(cfg, "check_inference"):
        planned = run.current_checks()
    else:
        inferred = infer_plan(run.goal, root)
        planned = list(inferred.checks)
        if planned and interactive:
            confirmed = confirm_plan(
                CheckPlan(run.goal, tuple(planned)), _read_plan_line, echo_status,
            )
            planned = list(confirmed.checks)
        elif planned:
            echo_status(format_plan(run.goal, tuple(planned)))
    payload = [item.as_dict() for item in planned]
    if not planned or not _choice_on(cfg, "until_baseline"):
        planned = _maybe_propose_checks(
            planned, cfg, model, run.goal, root, echo_status,
            memory_block=memory_block, clock_now=clock_now,
        )
        run.append("plan", "ready", checks=[item.as_dict() for item in planned])
        return
    echo_status(status_mod.msg_until_baseline())
    results = run_plan_commands(
        cfg, [item.cmd for item in planned], confirm_gate, root,
    )
    for row in results:
        echo_status(row["output"])
    outcome = apply_baseline(tuple(planned), results)
    for note in outcome.notes:
        echo_status(status_mod.msg_until_note(note))
    stored = outcome.checks if outcome.status == "ready" else tuple(planned)
    stored_list = list(stored)
    stored_list = _maybe_propose_checks(
        stored_list, cfg, model, run.goal, root, echo_status,
        memory_block=memory_block, clock_now=clock_now,
    )
    if len(stored_list) > len(stored):
        extra = stored_list[len(stored):]
        extra_results = run_plan_commands(
            cfg, [item.cmd for item in extra], confirm_gate, root,
        )
        for row in extra_results:
            echo_status(row["output"])
        results = list(results) + extra_results
    stored = tuple(stored_list)
    from .exec import backend_label
    run.append(
        "baseline", outcome.status,
        handoff="\n".join(outcome.notes),
        checks=[item.as_dict() for item in stored],
        results=results,
        backend=backend_label(),
    )
    reason = abstain_reason(outcome)
    if reason:
        run.append(
            "abstain", "infeasible",
            handoff="\n".join(outcome.notes),
            reason=reason,
            backend=backend_label(),
        )
        echo_status(status_mod.msg_until_abstain(reason))
        from . import usage
        usage.record("until.abstain", reason=reason)


def boundary_approval(policy, ask_gate, echo_status, label: str = "until") -> "tuple[list[str], list[str]]":
    """Ask once for the irreversible actions a GatePolicy denied this cycle.

    Returns the approved commands (now pre-approved on ``policy``), or [] when
    nothing was denied, no ``ask_gate`` is available (piped CLI), or the user
    said no. Never raises except KeyboardInterrupt (caller pauses the run).
    """
    if not isinstance(policy, tools.GatePolicy):
        return [], []
    policy.expire_approvals()  # the step they were approved for has ended
    denied = policy.take_denied()
    if not denied:
        return [], []
    echo_status(status_mod.msg_gates_denied(len(denied), label))
    for command in denied:
        echo_status("    " + command)
    if ask_gate is None or not ask_gate(status_mod.gates_denied_prompt(len(denied))):
        return [], list(denied)
    policy.approve(denied)
    return list(denied), []


def drop_denied(policy) -> None:
    """Forget denials from a read-only eval or a --check command (no re-ask)."""
    if isinstance(policy, tools.GatePolicy):
        policy.take_denied()


def approved_note(commands: "list[str]") -> str:
    if not commands:
        return ""
    return status_mod.APPROVED_NOTE.format(
        commands="\n".join("- " + c for c in commands),
    )


def _pause_interrupted(run: UntilRun, echo_status) -> UntilRun:
    if not run.is_paused() and not run.is_done():
        run.append(PAUSE_ROLE, "paused")
        echo_status(status_mod.msg_until_paused())
    return run


def run_until(
    cfg: dict, model: str, *,
    run: UntilRun,
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
    seed_handoff: str = "",
    clock_now=None,
    interactive: "bool | None" = None,
) -> UntilRun:
    """Advance ``run`` until pass, gate-no, pause, or interrupt. Mutates run.

    ``confirm_gate`` is wrapped in a ``tools.GatePolicy`` (``autonomous_gates``
    config): recoverable in-workspace file ops auto-approve; irreversible ones
    are denied during the maker step and asked once at the cycle boundary.
    """
    from . import usage

    usage.record("until.run", resume=bool(run.path.exists()))
    try:
        return _run_until_body(
            cfg, model, run=run, confirm_gate=confirm_gate, echo=echo,
            echo_status=echo_status, echo_tool=echo_tool, echo_error=echo_error,
            echo_round=echo_round, context_limit=context_limit,
            context_reserve=context_reserve, workspace_root=workspace_root,
            ask_gate=ask_gate, mine=mine, seed_handoff=seed_handoff,
            clock_now=clock_now, interactive=interactive,
        )
    finally:
        usage.record(
            "until.finish",
            outcome=_until_outcome_label(run.events),
            paused=run.is_paused(),
            done=run.is_done(),
        )
        from .knowledge_graph import record_workflow_run
        cmds = [item.cmd for item in run.current_checks()]
        record_workflow_run(
            cfg, run_key=run.path.stem, goal=run.goal or "", commands=cmds,
        )


def _run_until_body(
    cfg: dict, model: str, *,
    run: UntilRun,
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
    seed_handoff: str = "",
    clock_now=None,
    interactive: "bool | None" = None,
) -> UntilRun:
    if echo_status is None:
        echo_status = echo
    root = Path(workspace_root).resolve() if workspace_root else Path.cwd().resolve()
    do_mine = cfg_bool(cfg, "until_mine") and mine is not None
    max_steps = max(1, cfg_int(cfg, "until_max_steps"))
    makers_this_call = 0
    check_output = ""
    last = run.last_work()
    if last and last.get("role") == "check":
        check_output = last.get("handoff") or ""
    if clock_now is None:
        clock_now = datetime.now(timezone.utc)
    gate = tools.autonomous_gate(cfg, confirm_gate, echo_status)
    if isinstance(gate, tools.GatePolicy):
        gate.approve(run.approved_commands())  # resumed right after a yes
    if interactive is None:
        interactive = sys.stdin.isatty()
    memory_block = memory.context_block(cfg)
    eval_model = eval_model_name(cfg, model)
    token_budget = cfg_int(cfg, "run_token_budget")
    run_tokens = sum(
        int((ev.get("usage") or {}).get("total_tokens") or 0)
        for ev in run.events
    )
    changed_files: list[str] = []

    try:
        _prepare_plan(
            run, cfg, root, echo_status, interactive, gate,
            model=model, memory_block=memory_block, clock_now=clock_now,
        )
        while True:
            if run.is_done():
                return run
            role = run.next_role(do_mine=do_mine)
            if role is None:
                run.append("done", "pass")
                echo_status(status_mod.msg_until_done())
                return run
            if token_budget > 0 and run_tokens >= token_budget:
                run.append(PAUSE_ROLE, "paused")
                echo_status(status_mod.msg_until_token_budget())
                return run
            if role == "maker":
                if makers_this_call >= max_steps:
                    run.append(PAUSE_ROLE, "paused")
                    echo_status(status_mod.msg_until_max_steps())
                    return run
                makers_this_call += 1
                snap = snapshot.take_snapshot(
                    cfg, root,
                    run_label=run.path.stem,
                    step=makers_this_call,
                )
                echo_status(status_mod.msg_until_step("maker", makers_this_call, max_steps))
                prompt = MAKER_PROMPT.format(
                    goal=run.goal,
                    handoff=approved_note(run.approved_commands())
                    + (run.last_handoff() or seed_handoff or "(none)"),
                )
                step_stats: dict = {}
                result = isolated_act(
                    cfg, model, prompt,
                    confirm_gate=gate, echo=echo, echo_status=echo_status,
                    echo_error=echo_error, echo_tool=echo_tool, echo_round=echo_round,
                    context_limit=context_limit, context_reserve=context_reserve,
                    workspace_root=root, log_label="/until maker",
                    clock_now=clock_now, memory_block=memory_block,
                    usage_stats=step_stats,
                )
                if result is None:
                    return _pause_interrupted(run, echo_status)
                messages, session_log = result
                run_tokens += int(step_stats.get("total_tokens") or 0)
                try:
                    import subprocess
                    proc = subprocess.run(
                        ["git", "-C", str(root), "diff", "--name-only"],
                        capture_output=True, text=True, check=False,
                    )
                    if proc.returncode == 0:
                        changed_files = [
                            ln.strip() for ln in proc.stdout.splitlines() if ln.strip()
                        ]
                except OSError:
                    pass
                denied_log = list(gate.denied) if isinstance(gate, tools.GatePolicy) else []
                from .exec import backend_label
                run.append(
                    "maker", "next",
                    handoff=last_assistant(messages),
                    session=str(session_log),
                    snapshot_ref=snap.ref,
                    usage=dict(step_stats),
                    denied=denied_log or None,
                    backend=backend_label(),
                )
                approved, _unused = boundary_approval(gate, ask_gate, echo_status)
                if approved:
                    run.append(APPROVE_ROLE, "yes", handoff=json.dumps(approved))
                continue
            if role == "check":
                echo_status(status_mod.msg_until_step("check", makers_this_call, max_steps))
                planned = run.current_checks()
                if changed_files and makers_this_call > 0:
                    planned = list(order_cycle_checks(
                        tuple(planned), changed_files, root,
                    ))
                results = run_plan_commands(
                    cfg, [item.cmd for item in planned], gate, root,
                )
                for row in results:
                    echo_status(row["output"])
                check_output = _plan_handoff(results)
                status = judge_cycle(
                    tuple(planned), results,
                    baseline_on=any(
                        ev.get("role") == "baseline" for ev in run.events
                    ),
                    baseline=run.baseline_statuses(),
                )
                if status == "blocked":
                    blocked = next(
                        (row["cmd"] for row in results if row["status"] == "blocked"),
                        "",
                    )
                    echo_status(status_mod.msg_until_check_blocked(blocked))
                from .exec import backend_label
                run.append(
                    "check", status, handoff=check_output, results=results,
                    backend=backend_label(),
                )
                from . import usage

                blocked_n = sum(1 for row in results if row["status"] == "blocked")
                usage.record(
                    "check.cycle",
                    status=status,
                    checks=len(planned),
                    blocked=blocked_n,
                )
                if status == "blocked":
                    usage.record(
                        "check.blocked",
                        cmd=next(
                            (row["cmd"] for row in results if row["status"] == "blocked"),
                            "",
                        ),
                        exit_class="spawn",
                    )
                continue
            if role == "eval":
                echo_status(status_mod.msg_until_step("eval", makers_this_call, max_steps))
                last_session = run.session_paths()
                shell_ev = shell_evidence_table(
                    last_session[-1] if last_session else Path(),
                )
                prompt = EVAL_PROMPT.format(
                    goal=run.goal,
                    handoff=run.last_maker_handoff() or "(none)",
                    check_output=check_output or "(none)",
                    shell_evidence=shell_ev,
                )
                step_stats = {}
                result = isolated_act(
                    cfg, model, prompt,
                    confirm_gate=gate, echo=echo, echo_status=echo_status,
                    echo_error=echo_error, echo_tool=echo_tool, echo_round=echo_round,
                    context_limit=context_limit, context_reserve=context_reserve,
                    workspace_root=root, log_label="/until eval",
                    readonly=True,
                    max_rounds=eval_max_rounds(cfg),
                    clock_now=clock_now,
                    memory_block=memory_block,
                    usage_stats=step_stats,
                    model_override=eval_model,
                )
                drop_denied(gate)
                if result is None:
                    return _pause_interrupted(run, echo_status)
                messages, session_log = result
                text = last_assistant(messages)
                status = parse_eval_status(text)
                run_tokens += int(step_stats.get("total_tokens") or 0)
                run.append(
                    "eval", status,
                    handoff=text,
                    session=str(session_log),
                    usage=dict(step_stats),
                )
                continue
            if role == "gate":
                echo_status(status_mod.msg_until_blocked())
                ok = False
                if ask_gate is not None:
                    ok = bool(ask_gate("Blocked. Continue working toward the goal? [y/N] "))
                run.append("gate", "yes" if ok else "no")
                continue
            if role == "mine":
                echo_status(status_mod.msg_until_mining())
                paths = [p for p in run.session_paths() if p.exists()]
                mine(paths)
                run.append("mine", "next")
                echo_status(status_mod.msg_until_done())
                return run
            raise RuntimeError(f"unknown until role {role!r}")
    except KeyboardInterrupt:
        return _pause_interrupted(run, echo_status)

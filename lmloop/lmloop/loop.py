"""Goal loop: isolated maker act() until an external check or eval says done.

A loop is a while-statement. This module owns the stop condition — exit code or
an isolated checker thread — so the maker cannot grade its own work.
"""

import shlex
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import agent, memory, status as status_mod
from .config import project_dir
from .tools import run_shell, shell_confirm_flags

STATUS_LINE_PREFIX = "STATUS:"
EXIT_CODE_PREFIX = "[exit code:"
VALID_EVAL_STATUS = frozenset({"pass", "fail", "blocked"})
META_ROLE = "meta"
PAUSE_ROLE = "pause"
DONE_ROLES = frozenset({"mine", "done"})
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

End your reply with exactly one last line, one of:
STATUS: pass
STATUS: fail
STATUS: blocked

pass — the goal is met with cited evidence.
fail — more work is needed.
blocked — you cannot tell, or a human must decide.
"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    last = body.splitlines()[-1] if body else ""
    if last.startswith(EXIT_CODE_PREFIX):
        rest = last[len(EXIT_CODE_PREFIX):].strip().rstrip("]")
        try:
            code = int(rest)
        except ValueError:
            return "fail"
        return "pass" if code == 0 else "fail"
    return "fail"


def parse_until_args(words: list) -> "tuple[str, str | None, str | None]":
    """Return (goal, check_cmd, err). err is set when the line is invalid."""
    check_cmd = None
    rest: list[str] = []
    i = 0
    while i < len(words):
        w = words[i]
        if w == "--check":
            if i + 1 >= len(words):
                return "", None, "usage: until [--check <cmd>] <goal>"
            check_cmd = words[i + 1]
            i += 2
            continue
        rest.append(w)
        i += 1
    goal = " ".join(rest).strip()
    if not goal:
        return "", check_cmd, "usage: until [--check <cmd>] <goal>"
    return goal, check_cmd, None


def parse_until_arg_line(arg: str) -> "tuple[str, str | None, str | None]":
    """Parse a REPL /until argument line with shlex (quoted --check)."""
    try:
        words = shlex.split(arg or "")
    except ValueError as e:
        return "", None, str(e)
    return parse_until_args(words)


def isolated_act(
    cfg: dict, model: str, user_text: str, *,
    confirm_gate=None, echo=print, echo_status=None, echo_error=None,
    echo_tool=None, echo_round=None, context_limit: int = 0,
    context_reserve: int = 2048, workspace_root: "Path | None" = None,
    log_label: str = "",
) -> "tuple[list, Path] | None":
    """Run act() on a fresh thread. Returns (messages, session_log), or None.

    KeyboardInterrupt is logged, then re-raised so ``run_until`` can pause.
    """
    if echo_status is None:
        echo_status = echo
    if echo_error is None:
        echo_error = echo_status
    messages = [{"role": "system", "content": agent.system_prompt(cfg)}]
    session_log = memory.new_session_log()
    if log_label:
        memory.log_event(session_log, "user", log_label)
    messages.append({"role": "user", "content": user_text})
    try:
        agent.act(
            cfg, model, messages, session_log=session_log,
            confirm_gate=confirm_gate,
            echo=echo,
            echo_status=echo_status,
            echo_tool=echo_tool,
            echo_round=echo_round,
            context_limit=context_limit,
            context_reserve=context_reserve,
            workspace_root=workspace_root,
        )
    except agent.ServerError as e:
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
               slug: "str | None" = None) -> "UntilRun":
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        d = until_dir(slug)
        path = d / f"{ts}.jsonl"
        n = 1
        while path.exists():
            path = d / f"{ts}-{n}.jsonl"
            n += 1
        run = cls(path=path, goal=goal, check_cmd=check_cmd or None, events=[])
        run._write({
            "ts": _now(),
            "role": META_ROLE,
            "goal": goal,
            "check_cmd": check_cmd or "",
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
               session: str = "") -> None:
        step = sum(1 for e in self.events if e.get("role") not in (META_ROLE,))
        self._write({
            "ts": _now(),
            "step": step,
            "role": role,
            "status": status,
            "handoff": handoff,
            "session": session,
        })

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
            ("maker", "next"): "check" if self.check_cmd else "eval",
            ("check", "pass"): after_pass,
            ("check", "fail"): "eval",
            ("check", "blocked"): "gate",
            ("eval", "pass"): after_pass,
            ("eval", "fail"): "maker",
            ("eval", "blocked"): "gate",
            ("gate", "yes"): "maker",
            ("gate", "no"): None,
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


def _run_check(cfg: dict, command: str, confirm_gate, workspace_root: Path) -> str:
    destructive, syntax = shell_confirm_flags(cfg)
    return run_shell(
        command,
        confirm_gate=confirm_gate,
        timeout_s=int(cfg.get("shell_timeout_s") or 120),
        confirm_destructive=destructive,
        confirm_shell_syntax=syntax,
        workspace_root=workspace_root,
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
    context_reserve: int = 2048,
    workspace_root: "Path | None" = None,
    ask_gate=None,
    mine=None,
) -> UntilRun:
    """Advance ``run`` until pass, gate-no, pause, or interrupt. Mutates run."""
    if echo_status is None:
        echo_status = echo
    root = Path(workspace_root).resolve() if workspace_root else Path.cwd().resolve()
    do_mine = bool(cfg.get("until_mine", True)) and mine is not None
    max_steps = max(1, int(cfg.get("until_max_steps") or 12))
    makers_this_call = 0
    check_output = ""
    last = run.last_work()
    if last and last.get("role") == "check":
        check_output = last.get("handoff") or ""

    try:
        while True:
            if run.is_done():
                return run
            role = run.next_role(do_mine=do_mine)
            if role is None:
                run.append("done", "pass")
                echo_status(status_mod.msg_until_done())
                return run
            if role == "maker":
                if makers_this_call >= max_steps:
                    run.append(PAUSE_ROLE, "paused")
                    echo_status(status_mod.msg_until_paused())
                    return run
                makers_this_call += 1
                echo_status(status_mod.msg_until_step("maker", makers_this_call, max_steps))
                prompt = MAKER_PROMPT.format(
                    goal=run.goal,
                    handoff=run.last_handoff() or "(none)",
                )
                result = isolated_act(
                    cfg, model, prompt,
                    confirm_gate=confirm_gate, echo=echo, echo_status=echo_status,
                    echo_error=echo_error, echo_tool=echo_tool, echo_round=echo_round,
                    context_limit=context_limit, context_reserve=context_reserve,
                    workspace_root=root, log_label="/until maker",
                )
                if result is None:
                    return run
                messages, session_log = result
                run.append(
                    "maker", "next",
                    handoff=last_assistant(messages),
                    session=str(session_log),
                )
                continue
            if role == "check":
                echo_status(status_mod.msg_until_step("check", makers_this_call, max_steps))
                check_output = _run_check(cfg, run.check_cmd or "", confirm_gate, root)
                displayed = clip_check_output(check_output)
                echo_status(displayed)
                status = check_status_from_output(check_output)
                run.append("check", status, handoff=displayed)
                continue
            if role == "eval":
                echo_status(status_mod.msg_until_step("eval", makers_this_call, max_steps))
                prompt = EVAL_PROMPT.format(
                    goal=run.goal,
                    handoff=run.last_maker_handoff() or "(none)",
                    check_output=check_output or "(none)",
                )
                result = isolated_act(
                    cfg, model, prompt,
                    confirm_gate=confirm_gate, echo=echo, echo_status=echo_status,
                    echo_error=echo_error, echo_tool=echo_tool, echo_round=echo_round,
                    context_limit=context_limit, context_reserve=context_reserve,
                    workspace_root=root, log_label="/until eval",
                )
                if result is None:
                    return run
                messages, session_log = result
                text = last_assistant(messages)
                status = parse_eval_status(text)
                run.append(
                    "eval", status,
                    handoff=text,
                    session=str(session_log),
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

"""Implementation-plan runner: parse, ledger, red-green gate, one review.

The until machine stays in ``until_runner``. This module owns plan files
under ``docs/plans/`` and the ledger at
``~/.lmloop/plans/<slug>/<plan-id>/progress.json``. It does not import
``company`` or ``graph``. Callers that create worktrees pass a callback.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..core.config import STATE_ROOT, Config, project_slug
from ..prompts import skills
from ..verify.checks import infer_plan
from ..verify.snapshot import take_snapshot

NEED_DERIVED = "derived"
RULING_PREFIX = "Ruling:"
_BLOCKING = ("BLOCKER", "MAJOR", "CRITICAL", "IMPORTANT")
_MINOR = ("MINOR", "NIT")
_FINISH_CHOICES = ("merge", "pr", "keep")
DISCARD_WORD = "discard"


class PlanError(ValueError):
    """The plan file or a finish choice cannot be executed."""


@dataclass
class ShellSpan:
    """One ``run_shell`` result the gate can check."""

    command: str
    exit_code: int
    tail: str = ""

    def as_dict(self) -> dict:
        return {
            "command": self.command,
            "exit_code": self.exit_code,
            "tail": self.tail,
        }


@dataclass
class PlanStep:
    index: int
    title: str
    done: bool
    command: str
    expected: str
    is_red: bool
    is_green: bool


@dataclass
class PlanTask:
    index: int
    title: str
    steps: list
    markdown: str


@dataclass
class PlanDocument:
    path: str
    plan_id: str
    goal: str
    spec: str
    constraints: str
    review_focus: str
    tasks: list


@dataclass
class PlanResult:
    status: str
    summary: str
    ledger: Path
    minors: tuple = ()


@dataclass
class PlanHost:
    """Surfaces ``serve_plan`` needs. Git worktrees stay in the callback."""

    cfg: Config
    model: str
    workspace: Path
    echo: Callable
    error: Callable
    ask: Callable
    isatty: bool
    open_worktree: Callable
    run_check: Callable
    land: Callable
    discard: Callable
    linked: bool
    branch: str
    base: str
    act: Callable | None = None
    review_act: Callable | None = None
    main_checkout: Callable | None = None


def plan_id_for(path: Path) -> str:
    """Ledger key from the plan filename. Same stem shares a ledger."""
    cleaned = []
    for char in path.stem:
        if char.isalnum() or char in "-_.":
            cleaned.append(char)
        else:
            cleaned.append("-")
    ident = "".join(cleaned).strip("-")
    return ident or "plan"


def ledger_path(slug: str, plan_id: str) -> Path:
    return STATE_ROOT / "plans" / slug / plan_id / "progress.json"


def preference_path(slug: str) -> Path:
    return STATE_ROOT / "plans" / slug / "worktree.json"


def _read_preference(slug: str) -> dict:
    path = preference_path(slug)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_preference(slug: str, data: dict) -> None:
    path = preference_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data) + "\n", encoding="utf-8")


def load_worktree_preference(slug: str) -> str:
    """``yes`` or ``no`` remembered for this project, else empty."""
    value = str(_read_preference(slug).get("worktree") or "")
    return value if value in ("yes", "no") else ""


def save_worktree_preference(slug: str, value: str) -> None:
    if value not in ("yes", "no"):
        raise PlanError("worktree preference must be yes or no")
    data = _read_preference(slug)
    data["worktree"] = value
    _write_preference(slug, data)


def save_active_worktree(slug: str, path: Path, branch: str) -> None:
    """Remember the worktree this project last opened, for ``plan finish``."""
    data = _read_preference(slug)
    data["active_path"] = str(path)
    data["active_branch"] = branch
    _write_preference(slug, data)


def load_active_worktree(slug: str) -> "tuple[Path, str] | None":
    data = _read_preference(slug)
    raw = str(data.get("active_path") or "").strip()
    branch = str(data.get("active_branch") or "").strip()
    if not raw or not branch:
        return None
    return Path(raw), branch


def clear_active_worktree(slug: str) -> None:
    data = _read_preference(slug)
    data.pop("active_path", None)
    data.pop("active_branch", None)
    if data:
        _write_preference(slug, data)


def discard_refusal(word: str) -> str:
    """Empty when the user typed the exact discard word."""
    if word != DISCARD_WORD:
        return "discard requires the word discard"
    return ""


def finish_menu(base: str) -> str:
    return (
        "Implementation complete. What would you like to do?\n"
        f"1. Merge back to {base} locally\n"
        "2. Push and create a Pull Request\n"
        "3. Keep the branch as-is (I'll handle it later)\n"
        "Which option?"
    )


def choice_from_answer(text: str) -> str:
    raw = (text or "").strip().lower()
    return {
        "1": "merge",
        "2": "pr",
        "3": "keep",
        "merge": "merge",
        "pr": "pr",
        "keep": "keep",
    }.get(raw, "")


def normalize_cmd(command: str) -> str:
    return " ".join((command or "").split())


def exit_code_from_text(content: str) -> "int | None":
    """Last ``[exit code: N]`` line, or None when the tool did not finish."""
    found = None
    for line in (content or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("[exit code:") or not stripped.endswith("]"):
            continue
        inner = stripped[len("[exit code:"):-1].strip()
        try:
            found = int(inner)
        except ValueError:
            continue
    return found


def shell_evidence(messages: list) -> "list[ShellSpan]":
    """Pair ``run_shell`` calls with the tool results that follow them."""
    spans: list[ShellSpan] = []
    pending: list[str] = []
    for message in messages or []:
        role = message.get("role")
        if role == "assistant":
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                if function.get("name") != "run_shell":
                    continue
                raw = function.get("arguments") or "{}"
                try:
                    args = json.loads(raw) if isinstance(raw, str) else dict(raw)
                except json.JSONDecodeError:
                    args = {}
                pending.append(str(args.get("command") or ""))
            continue
        if role == "tool" and pending:
            command = pending.pop(0)
            content = str(message.get("content") or "")
            code = exit_code_from_text(content)
            if code is None:
                continue
            tail = content[-400:]
            spans.append(ShellSpan(command=command, exit_code=code, tail=tail))
    return spans


def parse_rulings(text: str) -> "list[str]":
    found = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith(RULING_PREFIX):
            found.append(stripped)
    return found


def waived_commands(rulings: list) -> "set[str]":
    """Command named before `` - `` on a Ruling line."""
    waived = set()
    for line in rulings:
        body = line[len(RULING_PREFIX):].strip()
        if " - " in body:
            body = body.split(" - ", 1)[0].strip()
        if body:
            waived.add(normalize_cmd(body))
    return waived


def _saw_fail_then_pass(command: str, spans: list) -> bool:
    want = normalize_cmd(command)
    failed = False
    for span in spans:
        if normalize_cmd(span.command) != want:
            continue
        if span.exit_code != 0:
            failed = True
        elif failed:
            return True
    return False


def _fresh_success(command: str, spans: list) -> bool:
    want = normalize_cmd(command)
    return any(
        normalize_cmd(span.command) == want and span.exit_code == 0
        for span in spans
    )


def gate_reason(task: PlanTask, spans: list, rulings: list) -> str:
    """Empty when the task may complete. ``derived`` asks for a project check.

    A ruling records a deviation. It waives that command's red-then-green
    pair and still requires some later exit 0.
    """
    waived = waived_commands(rulings)
    reds = [
        step for step in task.steps
        if step.is_red and step.command and normalize_cmd(step.command) not in waived
    ]
    for step in reds:
        if not _saw_fail_then_pass(step.command, spans):
            return f"RED step has no fail-then-pass evidence: {step.command}"
    greens = [
        step for step in task.steps
        if step.command and not step.is_red and normalize_cmd(step.command) not in waived
    ]
    if greens:
        for step in greens:
            if not _fresh_success(step.command, spans):
                return f"no fresh exit 0 for: {step.command}"
        return ""
    if reds:
        return ""
    if any(span.exit_code == 0 for span in spans) and waived:
        return ""
    return NEED_DERIVED


def fail_then_pass_any(spans: list) -> bool:
    """True when some command failed and that same command later passed."""
    failed: set[str] = set()
    for span in spans:
        key = normalize_cmd(span.command)
        if not key:
            continue
        if span.exit_code != 0:
            failed.add(key)
        elif key in failed:
            return True
    return False


def _strip_ticks(text: str) -> str:
    body = text.strip()
    if len(body) >= 2 and body[0] == "`" and body[-1] == "`":
        return body[1:-1].strip()
    return body


def _task_title(rest: str) -> str:
    text = rest.strip()
    index = 0
    while index < len(text) and text[index].isdigit():
        index += 1
    if index and index < len(text) and text[index] in ":.":
        text = text[index + 1:].strip()
    return text


def _step_title(rest: str) -> str:
    text = rest.strip()
    if text.startswith("**"):
        end = text.find("**", 2)
        if end != -1:
            text = text[2:end].strip()
    return text


def _is_red(title: str, expected: str) -> bool:
    title_l = title.lower()
    expected_l = expected.lower()
    if expected_l.startswith("fail"):
        return True
    return any(token in title_l for token in (
        "failing", "fails", "verify it fails", "verify red",
    ))


def _is_green(title: str, expected: str) -> bool:
    title_l = title.lower()
    expected_l = expected.lower()
    if expected_l.startswith("pass"):
        return True
    return any(token in title_l for token in (
        "passes", "verify it passes", "verify green",
    ))


def _section(lines: list, heading: str) -> str:
    body: list[str] = []
    capture = False
    want = heading.lower()
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            title = stripped.lstrip("#").strip()
            if capture:
                break
            if title.lower() == want:
                capture = True
            continue
        if capture:
            body.append(line)
    return "\n".join(body).strip()


def _field(lines: list, label: str) -> str:
    prefix = label.lower()
    for line in lines:
        stripped = line.strip().lstrip("*").strip()
        if stripped.lower().startswith(prefix):
            return stripped[len(label):].strip().lstrip("*").strip()
    return ""


def _parse_steps(lines: list) -> list:
    steps = []
    current = None
    fence = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            fence = not fence
            if current is not None:
                current["body"].append(line)
            continue
        if not fence and (stripped.startswith("- [ ]") or stripped.startswith("- [x]") or stripped.startswith("- [X]")):
            if current is not None:
                steps.append(_finish_step(len(steps) + 1, current))
            done = stripped.startswith("- [x]") or stripped.startswith("- [X]")
            current = {"title": _step_title(stripped[5:]), "done": done, "body": []}
            continue
        if current is not None:
            current["body"].append(line)
    if current is not None:
        steps.append(_finish_step(len(steps) + 1, current))
    return steps


def _finish_step(index: int, raw: dict) -> PlanStep:
    command = ""
    expected = ""
    fence = False
    for line in raw["body"]:
        stripped = line.strip()
        if stripped.startswith("```"):
            fence = not fence
            continue
        if fence:
            continue
        if stripped.startswith("Run:"):
            command = _strip_ticks(stripped[4:])
        elif stripped.startswith("Expected:"):
            expected = stripped[len("Expected:"):].strip()
    title = raw["title"]
    return PlanStep(
        index=index,
        title=title,
        done=bool(raw["done"]),
        command=command,
        expected=expected,
        is_red=_is_red(title, expected),
        is_green=_is_green(title, expected),
    )


def parse_plan(text: str, *, path: "Path | None" = None) -> PlanDocument:
    """Split a plan on ``### Task`` headings and checkbox steps.

    Line scanning only. A fenced block does not create a step or a ``Run:``.
    """
    lines = text.splitlines()
    header: list[str] = []
    chunks: list[dict] = []
    current = None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("### Task"):
            if current is not None:
                chunks.append(current)
            title = _task_title(stripped[len("### Task"):])
            if not title:
                raise PlanError("plan has a task the parser cannot name")
            current = {"title": title, "lines": [line]}
            continue
        if current is None:
            header.append(line)
        else:
            current["lines"].append(line)
    if current is not None:
        chunks.append(current)
    tasks = []
    for index, chunk in enumerate(chunks, start=1):
        body_lines = chunk["lines"][1:]
        tasks.append(PlanTask(
            index=index,
            title=chunk["title"],
            steps=_parse_steps(body_lines),
            markdown="\n".join(chunk["lines"]).strip() + "\n",
        ))
    plan_path = str(path.resolve()) if path is not None else ""
    return PlanDocument(
        path=plan_path,
        plan_id=plan_id_for(path) if path is not None else "plan",
        goal=_field(header, "Goal:"),
        spec=_field(header, "Spec:"),
        constraints=_section(header, "Global Constraints"),
        review_focus=_section(header, "Review Focus"),
        tasks=tasks,
    )


def _fresh_ledger(doc: PlanDocument) -> dict:
    return {
        "version": 1,
        "plan_path": doc.path,
        "plan_id": doc.plan_id,
        "tasks": {},
        "minors": [],
        "review": "",
    }


def load_ledger(path: Path, doc: PlanDocument) -> dict:
    if not path.is_file():
        return _fresh_ledger(doc)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _fresh_ledger(doc)
    if not isinstance(data, dict) or data.get("plan_path") != doc.path:
        return _fresh_ledger(doc)
    data.setdefault("tasks", {})
    data.setdefault("minors", [])
    data.setdefault("review", "")
    return data


def save_ledger(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def plan_status_text(slug: str, plan_id: str = "", plan_path: "Path | None" = None) -> str:
    """Human ledger. With no id, list every plan ledger for the project."""
    if plan_path is not None:
        plan_id = plan_id_for(plan_path)
    root = STATE_ROOT / "plans" / slug
    if not plan_id:
        if not root.is_dir():
            return "no plan ledgers"
        lines = []
        for child in sorted(root.iterdir()):
            ledger = child / "progress.json"
            if ledger.is_file():
                lines.append(plan_status_text(slug, plan_id=child.name))
        return "\n".join(lines) if lines else "no plan ledgers"
    path = ledger_path(slug, plan_id)
    if not path.is_file():
        return f"no plan ledger for {plan_id}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return f"plan ledger unreadable: {exc}"
    lines = [f"plan {plan_id} ({data.get('plan_path', '')})"]
    tasks = data.get("tasks") or {}

    def _key(item: str) -> int:
        return int(item) if str(item).isdigit() else 0

    for key in sorted(tasks, key=_key):
        row = tasks[key] or {}
        lines.append(f"  task {key}: {row.get('status', 'open')} {row.get('title', '')}".rstrip())
        if row.get("block"):
            lines.append(f"    {row['block']}")
    if data.get("review"):
        lines.append(f"  review: {data['review']}")
    for minor in data.get("minors") or []:
        lines.append(f"  minor: {minor}")
    return "\n".join(lines)


def last_assistant_text(messages: list) -> str:
    fallback = ""
    for message in reversed(messages or []):
        if message.get("role") != "assistant":
            continue
        content = str(message.get("content") or "")
        if content and not message.get("tool_calls"):
            return content
        if content and not fallback:
            fallback = content
    return fallback


def task_prompt(doc: PlanDocument, task: PlanTask) -> str:
    """This task plus the discipline skills. Earlier tasks stay out."""
    tdd = skills.load_skill("tdd", public_only=True)
    verify = skills.load_skill("verify", public_only=True)
    constraints = doc.constraints or "(none)"
    return (
        f"{tdd}\n\n{verify}\n\n"
        f"## Global constraints\n\n{constraints}\n\n"
        f"## This task only\n\n{task.markdown}\n"
        "When the plan is wrong, add `Ruling: <command> - <reason>`. "
        "A ruling does not replace a failing-then-passing command. "
        "Do not claim the task is done without a fresh exit code."
    )


def review_prompt(doc: PlanDocument, diff_stat: str) -> str:
    review = skills.load_skill("review", public_only=True)
    focus = doc.review_focus or "(none)"
    spec = doc.spec or "(none)"
    return (
        f"{review}\n\n"
        f"Spec: {spec}\n\n"
        f"## Review focus\n\n{focus}\n\n"
        f"## Diff stat\n\n{diff_stat or '(no diff)'}\n"
    )


def fix_prompt(doc: PlanDocument, findings: list) -> str:
    tdd = skills.load_skill("tdd", public_only=True)
    listed = "\n".join(f"- {line}" for line in findings)
    return (
        f"{tdd}\n\n"
        "One fix wave for these findings. For each one, write a failing test, "
        "watch it fail, then make it pass, then run the suite. "
        "Do not start a second review.\n\n"
        f"{listed}\n\n"
        f"## Global constraints\n\n{doc.constraints or '(none)'}\n"
    )


def classify_findings(text: str) -> "tuple[list[str], list[str]]":
    blocking: list[str] = []
    minors: list[str] = []
    for line in (text or "").splitlines():
        stripped = line.strip().lstrip("-").strip()
        if not stripped:
            continue
        token = stripped.split(None, 1)[0].upper().rstrip(":")
        if token in _BLOCKING:
            blocking.append(stripped)
        elif token in _MINOR:
            minors.append(stripped)
    return blocking, minors


def _diff_stat(workspace: Path) -> str:
    import subprocess
    try:
        proc = subprocess.run(
            ["git", "-C", str(workspace), "diff", "--stat", "HEAD"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (proc.stdout or proc.stderr or "").strip()


def _derived_command(goal: str, workspace: Path) -> str:
    plan = infer_plan(goal or "implement the plan", workspace)
    commands = plan.commands()
    return commands[0] if commands else ""


def _span_from_output(command: str, output: str) -> ShellSpan:
    code = exit_code_from_text(output)
    if code is None:
        code = 1 if output.startswith("ERROR:") else 0
    return ShellSpan(command=command, exit_code=code, tail=output[-400:])


def default_run_check(command: str, workspace: Path) -> str:
    """Run one verification command on the host and return the tool text."""
    return _default_check(command, workspace)


def _default_check(command: str, workspace: Path) -> str:
    from ..capabilities.tools import run_shell
    return run_shell(
        command, workspace_root=workspace, confirm_destructive=False,
    )


def _fresh_act(cfg: Config, model: str, workspace: Path):
    def act(prompt: str) -> list:
        from .until_runner import isolated_act
        result = isolated_act(
            cfg, model, prompt, workspace_root=workspace, log_label="plan",
        )
        if not result:
            return []
        messages, _log = result
        return messages
    return act


def _record_task(state: dict, task: PlanTask, *, status: str, spans: list,
                 rulings: list, block: str) -> None:
    state["tasks"][str(task.index)] = {
        "status": status,
        "title": task.title,
        "block": block,
        "rulings": list(rulings),
        "evidence": [span.as_dict() for span in spans],
    }


def _final_summary(doc: PlanDocument, minors: list, status: str) -> str:
    lines = [f"plan {doc.plan_id}: {status}"]
    if minors:
        lines.append("Deferred minors:")
        lines.extend(f"- {line}" for line in minors)
    return "\n".join(lines)


def run_plan(
    cfg: Config,
    model: str,
    plan_path: Path,
    *,
    workspace: Path,
    slug: "str | None" = None,
    act=None,
    review_act=None,
    run_command=None,
    snapshot=None,
    echo=None,
) -> PlanResult:
    """Execute every open task, then one fresh review and at most one fix wave.

    ``act`` and ``review_act`` take a prompt and return a message list.
    Completed ledger rows are not sent to the model again.
    """
    path = Path(plan_path)
    if not path.is_file():
        raise PlanError(f"no plan file at {path}")
    doc = parse_plan(path.read_text(encoding="utf-8"), path=path)
    if not doc.tasks:
        raise PlanError("plan has no tasks")
    project = slug or project_slug()
    ledger_file = ledger_path(project, doc.plan_id)
    state = load_ledger(ledger_file, doc)
    say = echo or (lambda *_args: None)
    task_act = act or _fresh_act(cfg, model, workspace)
    reviewer = review_act or task_act
    check = run_command or (lambda command: _default_check(command, workspace))
    if snapshot is False:
        snap = None
    elif snapshot is None:
        snap = take_snapshot
    else:
        snap = snapshot

    for task in doc.tasks:
        row = (state.get("tasks") or {}).get(str(task.index)) or {}
        if row.get("status") == "complete":
            continue
        if snap is not None:
            snap(cfg, workspace, run_label=f"plan-{doc.plan_id}", step=task.index)
        messages = task_act(task_prompt(doc, task)) or []
        spans = shell_evidence(messages)
        text = last_assistant_text(messages)
        rulings = parse_rulings(text)
        reason = gate_reason(task, spans, rulings)
        if reason == NEED_DERIVED:
            command = _derived_command(doc.goal, workspace)
            if not command:
                reason = "no verification command in the plan or the project"
            else:
                span = _span_from_output(command, check(command))
                spans = list(spans) + [span]
                reason = "" if span.exit_code == 0 else f"derived check failed: {command}"
        if reason:
            _record_task(
                state, task, status="blocked", spans=spans, rulings=rulings, block=reason,
            )
            save_ledger(ledger_file, state)
            say(reason)
            return PlanResult(status="blocked", summary=reason, ledger=ledger_file)
        _record_task(
            state, task, status="complete", spans=spans, rulings=rulings, block="",
        )
        save_ledger(ledger_file, state)
        say(f"task {task.index} complete")

    if state.get("review") == "clean":
        summary = _final_summary(doc, list(state.get("minors") or []), "done")
        return PlanResult(
            status="done", summary=summary, ledger=ledger_file,
            minors=tuple(state.get("minors") or ()),
        )

    review_messages = reviewer(review_prompt(doc, _diff_stat(workspace))) or []
    blocking, minors = classify_findings(last_assistant_text(review_messages))
    state["minors"] = minors
    if blocking:
        fix_messages = task_act(fix_prompt(doc, blocking)) or []
        fix_spans = shell_evidence(fix_messages)
        if not fail_then_pass_any(fix_spans):
            state["review"] = "blocked"
            state["fix_evidence"] = [span.as_dict() for span in fix_spans]
            save_ledger(ledger_file, state)
            summary = "fix wave lacked red-green evidence"
            say(summary)
            return PlanResult(
                status="review_blocked", summary=summary, ledger=ledger_file,
                minors=tuple(minors),
            )
        state["fix_evidence"] = [span.as_dict() for span in fix_spans]
    state["review"] = "clean"
    save_ledger(ledger_file, state)
    summary = _final_summary(doc, minors, "done")
    say(summary)
    return PlanResult(
        status="done", summary=summary, ledger=ledger_file, minors=tuple(minors),
    )


def _worktree_choice(request, host: PlanHost, slug: str) -> str:
    choice = request.worktree
    if choice:
        return choice
    remembered = load_worktree_preference(slug)
    if remembered:
        return remembered
    if host.linked:
        return "no"
    if not host.isatty:
        return "no"
    answer = bool(host.ask(
        "Set up an isolated worktree? It protects your current branch. [y/N] "
    ))
    picked = "yes" if answer else "no"
    save_worktree_preference(slug, picked)
    return picked


def serve_plan(request, host: PlanHost) -> int:
    """CLI and REPL entry. ``request`` is a ``PlanRequest``."""
    slug = project_slug()
    if request.action == "status":
        path = Path(request.path) if request.path else None
        host.echo(plan_status_text(slug, plan_path=path))
        return 0
    if request.action == "finish":
        return _serve_finish(request, host)
    if request.action != "run":
        host.error("usage: plan run <path> | status [path] | finish")
        return 2
    workspace = host.workspace
    choice = _worktree_choice(request, host, slug)
    if choice == "no":
        clear_active_worktree(slug)
    elif choice == "yes" and not host.linked:
        name = plan_id_for(Path(request.path))
        try:
            workspace = host.open_worktree(host.workspace, name)
        except (OSError, RuntimeError) as exc:
            host.error(str(exc))
            return 1
        save_active_worktree(slug, workspace, f"lmloop/{name}")
        host.echo(f"worktree {workspace}")
        if not _baseline_allows(request, host, workspace):
            return 1
    result = run_plan(
        host.cfg, host.model, Path(request.path),
        workspace=workspace, slug=slug,
        act=host.act, review_act=host.review_act,
        echo=host.echo,
    )
    host.echo(result.summary)
    return 0 if result.status == "done" else 1


def _baseline_allows(request, host: PlanHost, workspace: Path) -> bool:
    command = _derived_command("plan baseline", workspace)
    if not command:
        host.echo("no project check to baseline")
        return True
    host.echo(f"baseline: {command}")
    output = host.run_check(command, workspace)
    if exit_code_from_text(output) == 0:
        return True
    host.error(output[-2000:])
    if request.allow_red_baseline:
        return True
    if host.isatty and host.ask("Baseline is red. Continue anyway? [y/N] "):
        return True
    if not host.isatty:
        host.error("baseline is red; pass --allow-red-baseline to continue")
    return False


def _optional_path(request) -> "Path | None":
    raw = str(getattr(request, "worktree_path", "") or "").strip()
    if not raw:
        return None
    return Path(raw)


def _finish_target(request, host: PlanHost, slug: str):
    """Repo to land in, feature branch, and the worktree path to remove."""
    path = _optional_path(request)
    branch = host.branch
    repo = host.workspace
    active = load_active_worktree(slug)
    if path is None and active is not None and not host.linked:
        path, active_branch = active
        if active_branch:
            branch = active_branch
    if host.linked and host.main_checkout is not None:
        main = host.main_checkout()
        if main is not None and Path(main).resolve() != host.workspace.resolve():
            if path is None:
                path = host.workspace
            repo = Path(main)
    return repo, branch, path


def _serve_finish(request, host: PlanHost) -> int:
    slug = project_slug()
    repo, branch, worktree_path = _finish_target(request, host, slug)
    if request.discard:
        refusal = discard_refusal(request.discard)
        if refusal:
            host.error(refusal)
            return 2
        ok, detail = host.discard(repo, branch, worktree_path)
        if ok:
            clear_active_worktree(slug)
            host.echo(detail)
            return 0
        host.error(detail)
        return 1
    choice = request.choice
    if not choice:
        host.echo(finish_menu(host.base))
        if not host.isatty:
            host.error("pass --choice merge|pr|keep")
            return 2
        try:
            answer = input("")
        except (EOFError, KeyboardInterrupt):
            host.error("no finish choice")
            return 2
        choice = choice_from_answer(answer)
    if choice not in _FINISH_CHOICES:
        host.error("finish choice must be merge, pr, or keep")
        return 2
    ok, detail = host.land(
        repo, choice, base=host.base, branch=branch,
        worktree_path=worktree_path,
    )
    if ok and choice == "merge":
        clear_active_worktree(slug)
    if ok:
        host.echo(detail)
        return 0
    host.error(detail)
    return 1

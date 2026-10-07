"""Derive a check plan from a goal and the workspace.

Deterministic: file reads and shell tokenization only. No model calls.
``loop.py`` runs the plan, baselines it, and decides pass / fail / eval.
"""

import shlex
import shutil
from dataclasses import dataclass, replace
from pathlib import Path

from . import memory
from .tools import ShellCommand

TIER_AUTHORITATIVE = "authoritative"
TIER_ADVISORY = "advisory"
ROLE_CHECK = "check"
ROLE_KEEP = "keep"

_KEEP_CUES = (
    ("without breaking", ROLE_KEEP),
    ("don't break", ROLE_KEEP),
    ("dont break", ROLE_KEEP),
    ("keep", ROLE_KEEP),
    ("still", ROLE_KEEP),
    ("run", ROLE_CHECK),
    ("make", ROLE_CHECK),
    ("until", ROLE_CHECK),
)
_DOC_NAMES = (
    "DEVELOPMENT.md", "CONTRIBUTING.md", "AGENTS.md", "CLAUDE.md", "README.md",
)
_SKIP_DIRS = frozenset({
    "node_modules", ".venv", "venv", "dist", "build", ".git",
    "__pycache__", "site-packages",
})
_HEADING_MARKS = ("test", "lint", "check", "verify", "qa")
_FENCE_LANGS = frozenset({"bash", "sh", "console", "shell", "zsh"})
_STRONG_RUNNERS = frozenset({
    "pytest", "unittest", "ruff", "mypy", "tsc", "eslint", "pyright",
    "vitest", "jest", "tox", "ctest", "deno",
})
_AMBIGUOUS_RUNNERS = frozenset({"test", "lint", "check"})
_RUNNER_PARENTS = frozenset({
    "npm", "pnpm", "yarn", "bun", "make", "gmake", "just", "pre-commit",
    "python", "python3",
})
_RUNNER_PAIRS = frozenset({
    ("cargo", "test"), ("go", "test"), ("npm", "test"), ("pnpm", "test"),
    ("yarn", "test"), ("bun", "test"), ("mix", "test"),
})
_NPM_SCRIPTS = ("test", "lint", "typecheck", "build")
_PROJECT_MARKERS = {
    "make": ("Makefile", "makefile", "GNUmakefile"),
    "gmake": ("Makefile", "makefile", "GNUmakefile"),
    "just": ("justfile", "Justfile", ".justfile"),
    "npm": ("package.json",),
    "pnpm": ("package.json",),
    "yarn": ("package.json",),
    "bun": ("package.json",),
    "cargo": ("Cargo.toml",),
    "go": ("go.mod",),
    "mix": ("mix.exs",),
    "deno": ("deno.json", "deno.jsonc"),
    "pytest": ("pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini"),
    "pre-commit": (".pre-commit-config.yaml",),
    "mvn": ("pom.xml",),
    "gradle": ("build.gradle", "build.gradle.kts"),
}
_DOC_LIMIT = 200_000
_PLAN_PROMPT = "  Enter run · e edit · s skip checks (checker only)"


@dataclass(frozen=True)
class PlannedCheck:
    """One command the until loop may run. Tier never comes from a model."""

    cmd: str
    role: str
    source: str
    tier: str
    reason: str
    user_typed: bool = False
    proves: bool = True

    def as_dict(self) -> dict:
        return {
            "cmd": self.cmd,
            "role": self.role,
            "source": self.source,
            "tier": self.tier,
            "reason": self.reason,
            "user_typed": self.user_typed,
            "proves": self.proves,
        }

    @classmethod
    def from_dict(cls, row: dict) -> "PlannedCheck":
        return cls(
            cmd=str(row.get("cmd") or ""),
            role=row.get("role") or ROLE_CHECK,
            source=str(row.get("source") or ""),
            tier=row.get("tier") or TIER_AUTHORITATIVE,
            reason=str(row.get("reason") or ""),
            user_typed=bool(row.get("user_typed")),
            proves=bool(row.get("proves", True)),
        )


@dataclass(frozen=True)
class CheckPlan:
    goal: str
    checks: tuple

    def commands(self) -> tuple:
        return tuple(item.cmd for item in self.checks)


@dataclass(frozen=True)
class BaselineOutcome:
    checks: tuple
    status: str
    notes: tuple


def flags_plan(checks: tuple, keeps: tuple) -> tuple:
    """Explicit ``--check`` / ``--keep`` commands. Inference must not add more."""
    rows = []
    for cmd in checks:
        rows.append(PlannedCheck(
            cmd=cmd, role=ROLE_CHECK, source="flag", tier=TIER_AUTHORITATIVE,
            reason="--check", user_typed=True,
        ))
    for cmd in keeps:
        rows.append(PlannedCheck(
            cmd=cmd, role=ROLE_KEEP, source="flag", tier=TIER_AUTHORITATIVE,
            reason="--keep", user_typed=True,
        ))
    return tuple(rows)


def format_plan(goal: str, checks: tuple) -> str:
    """The one-screen plan shown before the first maker step."""
    lines = [f'until · check plan for "{goal}"']
    for item in checks:
        label = "advisory" if item.tier == TIER_ADVISORY else item.role
        lines.append(f"  {label:<8} {item.cmd}")
        if item.reason:
            lines.append(f"           {item.reason}")
    lines.append(_PLAN_PROMPT)
    return "\n".join(lines)


def confirm_plan(plan: CheckPlan, read_line, echo) -> CheckPlan:
    """Enter runs the plan, ``e`` edits it, ``s`` skips to the checker."""
    echo(format_plan(plan.goal, plan.checks))
    try:
        choice = (read_line("") or "").strip().lower()
    except KeyboardInterrupt:
        raise
    if choice in ("s", "skip"):
        return CheckPlan(plan.goal, ())
    if choice not in ("e", "edit"):
        return plan
    echo(
        "one command per line; start a line with 'keep ' for an invariant; "
        "blank line ends"
    )
    rows = []
    while True:
        line = read_line("check> ")
        if line is None or not str(line).strip():
            break
        text = str(line).strip()
        role = ROLE_CHECK
        if text.startswith("keep "):
            role = ROLE_KEEP
            text = text[5:].strip()
        if not text or not command_parses(text):
            echo(f"ignored unparsable command: {text}")
            continue
        rows.append(PlannedCheck(
            cmd=text, role=role, source="user", tier=TIER_AUTHORITATIVE,
            reason="edited", user_typed=True,
        ))
    return CheckPlan(plan.goal, tuple(rows))


def infer_plan(goal: str, root: Path, *, learnings=None, history=None) -> CheckPlan:
    """Goal text, then project files, memory, and proven history.

    Earlier sources win on the same argv. A proven history command that is
    not already in the plan is placed first: it has already discriminated.
    """
    root = Path(root)
    goal_rows = _goal_checks(goal or "", root)
    project_rows = _project_checks(root)
    memory_rows = _memory_checks(learnings)
    history_rows = _history_checks(history)
    merged = _merge(goal_rows, project_rows, memory_rows, history_rows)
    return CheckPlan(goal or "", tuple(merged))


def apply_baseline(checks: tuple, results: list) -> BaselineOutcome:
    """Drop unrunnable inferred commands, block unrunnable typed ones, reclassify.

    An inferred check that already passes becomes a keep. A user-typed check
    that already passes stays a check but cannot prove the change (``proves``
    is false). A keep that already fails blocks the run.
    """
    by_cmd = {row.get("cmd"): row for row in results}
    kept = []
    notes = []
    for item in checks:
        row = by_cmd.get(item.cmd) or {}
        status = row.get("status") or "fail"
        if status == "blocked":
            if item.user_typed:
                return BaselineOutcome(
                    (), "blocked",
                    (f"check not runnable here: {item.cmd}",),
                )
            notes.append(f"dropped unrunnable check: {item.cmd}")
            continue
        if item.role == ROLE_KEEP and status != "pass":
            return BaselineOutcome(
                (), "blocked",
                (f"invariant already broken before any work: {item.cmd}",),
            )
        if (
            item.role == ROLE_CHECK
            and status == "pass"
            and item.tier == TIER_AUTHORITATIVE
            and not item.user_typed
        ):
            kept.append(replace(
                item, role=ROLE_KEEP,
                reason=_join_reason(item.reason, "already passing"),
            ))
            notes.append(f"already passing, kept as invariant: {item.cmd}")
            continue
        if item.role == ROLE_CHECK and status == "pass" and item.user_typed:
            kept.append(replace(item, proves=False))
            notes.append(
                f"this check already passes, so it cannot show the change: {item.cmd}"
            )
            continue
        if (
            item.tier == TIER_ADVISORY
            and item.role == ROLE_CHECK
            and status == "pass"
        ):
            kept.append(replace(item, role=ROLE_KEEP))
            continue
        kept.append(item)
    return BaselineOutcome(tuple(kept), "ready", tuple(notes))


def judge_cycle(checks: tuple, results: list, *, baseline_on: bool,
                baseline: dict) -> str:
    """Return pass, fail, blocked, or pending (eval must also judge).

    A pass is deterministic proof: an authoritative check went fail → pass
    (or any authoritative check passed when the baseline is off), and every
    keep and advisory command passed. Advisory success never finishes a run.
    """
    by_cmd = {row.get("cmd"): row.get("status") or "fail" for row in results}
    if any(by_cmd.get(item.cmd) == "blocked" for item in checks):
        return "blocked"
    for item in checks:
        if by_cmd.get(item.cmd, "fail") != "pass":
            return "fail"
    if _has_proof(checks, by_cmd, baseline_on=baseline_on, baseline=baseline):
        return "pass"
    return "pending"


def _has_proof(checks: tuple, by_cmd: dict, *, baseline_on: bool,
               baseline: dict) -> bool:
    for item in checks:
        if item.tier != TIER_AUTHORITATIVE or item.role != ROLE_CHECK:
            continue
        if not item.proves:
            continue
        if by_cmd.get(item.cmd) != "pass":
            continue
        if not baseline_on:
            return True
        if baseline.get(item.cmd) == "fail":
            return True
    return False


def _merge(goal, project, remembered, history) -> list:
    seen = set()
    ordered = []
    for group in (goal, project, remembered):
        for item in group:
            key = _norm(item.cmd)
            if not key or key in seen:
                continue
            seen.add(key)
            ordered.append(item)
    front = []
    for item in history:
        key = _norm(item.cmd)
        if not key or key in seen:
            continue
        seen.add(key)
        front.append(item)
    return front + ordered


def _norm(cmd: str):
    argv = _argv(cmd)
    if not argv:
        return None
    return tuple(argv)


def _argv(cmd: str):
    try:
        argv = shlex.split(cmd or "", posix=True)
    except ValueError:
        return None
    return argv or None


def _join_reason(reason: str, extra: str) -> str:
    if not reason:
        return extra
    return f"{reason} ({extra})"


def _goal_checks(goal: str, root: Path) -> list:
    rows = []
    occupied = []
    for start, end, cmd in _backtick_spans(goal):
        occupied.append((start, end))
        item = _goal_command(cmd, goal, start, root, source="goal")
        if item is not None:
            rows.append(item)
    for start, end, cmd in _quoted_spans(goal):
        if _inside(start, occupied):
            continue
        window = goal[max(0, start - 60):start]
        if _cue_role(window) is None:
            continue
        item = _goal_command(cmd, goal, start, root, source="goal")
        if item is not None:
            rows.append(item)
        occupied.append((start, end))
    rows.extend(_bare_targets(_blank(goal, occupied), root))
    return rows


def _goal_command(cmd: str, goal: str, pos: int, root: Path, source: str):
    if not command_parses(cmd):
        return None
    argv = ShellCommand(cmd).argv
    if not argv or not _resolves(argv[0], root):
        return None
    role = _cue_role(goal[max(0, pos - 60):pos]) or ROLE_CHECK
    return PlannedCheck(
        cmd=cmd.strip(), role=role, source=source, tier=TIER_AUTHORITATIVE,
        reason="from the goal",
    )


def _bare_targets(text: str, root: Path) -> list:
    words = text.split()
    rows = []
    i = 0
    while i < len(words):
        word = words[i]
        if word in ("make", "gmake", "just") and i + 1 < len(words):
            target = words[i + 1].strip(".,:;")
            if _make_target_exists(root, word, target):
                rows.append(PlannedCheck(
                    cmd=f"{word} {target}", role=_bare_role(words, i),
                    source="goal", tier=TIER_AUTHORITATIVE,
                    reason=f"from the goal ({target}: exists)",
                ))
                i += 2
                continue
        if word in ("npm", "pnpm", "yarn", "bun") and i + 1 < len(words):
            script, consumed = _npm_script_at(words, i, root)
            if script:
                rows.append(PlannedCheck(
                    cmd=f"{word} run {script}", role=_bare_role(words, i),
                    source="goal", tier=TIER_AUTHORITATIVE,
                    reason=f"from the goal (script {script})",
                ))
                i += consumed
                continue
        i += 1
    return rows


def _npm_script_at(words: list, index: int, root: Path):
    scripts = _package_scripts(root)
    nxt = words[index + 1].strip(".,:;")
    if nxt == "run" and index + 2 < len(words):
        script = words[index + 2].strip(".,:;")
        if script in scripts:
            return script, 3
        return None, 0
    if nxt in scripts:
        return nxt, 2
    return None, 0


def _project_checks(root: Path) -> list:
    rows = []
    rows.extend(_python_checks(root))
    rows.extend(_node_checks(root))
    rows.extend(_make_checks(root))
    rows.extend(_presence_checks(root))
    rows.extend(_precommit_checks(root))
    rows.extend(_ci_checks(root))
    rows.extend(_doc_checks(root))
    return rows


def _python_checks(root: Path) -> list:
    reasons = []
    if (root / "pytest.ini").is_file():
        reasons.append("pytest.ini")
    if (root / "tox.ini").is_file():
        reasons.append("tox.ini")
    if _cfg_has_pytest(root / "setup.cfg"):
        reasons.append("setup.cfg")
    if _pyproject_has_pytest(root / "pyproject.toml"):
        reasons.append("pyproject.toml")
    if not reasons:
        return []
    return [PlannedCheck(
        cmd=_pytest_cmd(root), role=ROLE_CHECK, source="project",
        tier=TIER_AUTHORITATIVE, reason=f"from {reasons[0]}",
    )]


def _pytest_cmd(root: Path) -> str:
    for rel in (".venv/bin/python", "venv/bin/python"):
        if (root / rel).is_file():
            return f"{rel} -m pytest -q"
    if shutil.which("python3"):
        return "python3 -m pytest -q"
    return "python -m pytest -q"


def _pyproject_has_pytest(path: Path) -> bool:
    if not path.is_file():
        return False
    text = _read_capped(path)
    import tomllib

    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        data = None
    tool = data.get("tool") if isinstance(data, dict) else None
    if isinstance(tool, dict) and "pytest" in tool:
        return True
    for line in text.splitlines():
        if line.strip().startswith("[tool.pytest"):
            return True
    return False


def _cfg_has_pytest(path: Path) -> bool:
    if not path.is_file():
        return False
    for line in _read_capped(path).splitlines():
        if line.strip().lower() in ("[pytest]", "[tool:pytest]"):
            return True
    return False


def _node_checks(root: Path) -> list:
    scripts = _package_scripts(root)
    if not scripts:
        return []
    pm = _package_manager(root)
    rows = []
    for name in _NPM_SCRIPTS:
        if name in scripts:
            rows.append(PlannedCheck(
                cmd=f"{pm} run {name}", role=ROLE_CHECK, source="project",
                tier=TIER_AUTHORITATIVE, reason=f"from package.json scripts.{name}",
            ))
    return rows


def _package_scripts(root: Path) -> dict:
    path = root / "package.json"
    if not path.is_file():
        return {}
    try:
        import json
        data = json.loads(_read_capped(path))
    except (OSError, ValueError):
        return {}
    scripts = data.get("scripts") if isinstance(data, dict) else None
    if not isinstance(scripts, dict):
        return {}
    return {str(k): v for k, v in scripts.items() if isinstance(k, str)}


def _package_manager(root: Path) -> str:
    if (root / "pnpm-lock.yaml").is_file():
        return "pnpm"
    if (root / "yarn.lock").is_file():
        return "yarn"
    if (root / "bun.lock").is_file() or (root / "bun.lockb").is_file():
        return "bun"
    return "npm"


def _make_checks(root: Path) -> list:
    rows = []
    for runner, names in (("make", ("Makefile", "makefile", "GNUmakefile")),
                          ("just", ("justfile", "Justfile", ".justfile"))):
        path = _first_file(root, names)
        if path is None:
            continue
        have = _recipe_targets(path)
        for target in ("test", "check", "lint"):
            if target in have:
                rows.append(PlannedCheck(
                    cmd=f"{runner} {target}", role=ROLE_CHECK, source="project",
                    tier=TIER_AUTHORITATIVE, reason=f"from {path.name} {target}:",
                ))
    return rows


def _bare_role(words: list, index: int) -> str:
    window = " ".join(words[max(0, index - 4):index])
    return _cue_role(window) or ROLE_CHECK


def _make_target_exists(root: Path, runner: str, target: str) -> bool:
    if not target or target.startswith("-"):
        return False
    path = _first_file(root, _PROJECT_MARKERS.get(runner, ()))
    if path is None:
        return False
    return target in _recipe_targets(path)


def _recipe_targets(path: Path) -> set:
    found = set()
    for line in _read_capped(path).splitlines():
        if not line or line[0].isspace() or line.startswith("#") or line.startswith("."):
            continue
        if ":" not in line:
            continue
        name = line.split(":", 1)[0]
        if "=" in name:
            continue
        parts = name.split()
        if parts:
            found.add(parts[0])
    return found


def _presence_checks(root: Path) -> list:
    specs = (
        (("Cargo.toml",), "cargo test", "Cargo.toml"),
        (("go.mod",), "go test ./...", "go.mod"),
        (("mix.exs",), "mix test", "mix.exs"),
        (("deno.json", "deno.jsonc"), "deno test", "deno.json"),
        (("gradlew",), "./gradlew test", "gradlew"),
        (("pom.xml",), "mvn test", "pom.xml"),
    )
    rows = []
    for names, cmd, reason in specs:
        if _first_file(root, names) is not None:
            rows.append(PlannedCheck(
                cmd=cmd, role=ROLE_CHECK, source="project",
                tier=TIER_AUTHORITATIVE, reason=f"from {reason}",
            ))
    if _first_file(root, ("Gemfile",)) and _first_file(root, ("Rakefile",)):
        rows.append(PlannedCheck(
            cmd="bundle exec rake test", role=ROLE_CHECK, source="project",
            tier=TIER_AUTHORITATIVE, reason="from Gemfile + Rakefile",
        ))
    if (root / "mvnw").is_file() and (root / "pom.xml").is_file():
        rows = [
            PlannedCheck(
                cmd="./mvnw test", role=ROLE_CHECK, source="project",
                tier=TIER_AUTHORITATIVE, reason="from mvnw",
            ) if item.cmd == "mvn test" else item
            for item in rows
        ]
    return rows


def _precommit_checks(root: Path) -> list:
    if not (root / ".pre-commit-config.yaml").is_file():
        return []
    return [PlannedCheck(
        cmd="pre-commit run --all-files", role=ROLE_KEEP, source="project",
        tier=TIER_AUTHORITATIVE, reason="from .pre-commit-config.yaml",
    )]


def _ci_checks(root: Path) -> list:
    wf = root / ".github" / "workflows"
    if not wf.is_dir():
        return []
    rows = []
    paths = sorted(wf.glob("*.yml")) + sorted(wf.glob("*.yaml"))
    for path in paths:
        for cmd in _workflow_runs(path):
            argv = _argv(cmd)
            if not argv:
                continue
            tier = TIER_AUTHORITATIVE if _resolves(argv[0], root) else TIER_ADVISORY
            rows.append(PlannedCheck(
                cmd=cmd, role=ROLE_CHECK, source="ci", tier=tier,
                reason=f"from {path.name}",
            ))
    return rows


def _workflow_runs(path: Path) -> list:
    runs = []
    skip_job = False
    for line in _read_capped(path).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent <= 2 and stripped.endswith(":") and not stripped.startswith("-"):
            skip_job = False
        if stripped == "services:" or stripped.startswith("services:"):
            skip_job = True
            continue
        if stripped.startswith("- "):
            stripped = stripped[2:].strip()
        if not stripped.startswith("run:"):
            continue
        value = stripped[4:].strip()
        if not value or value[0] in "|+>":
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if skip_job or "${{" in value or "secrets." in value:
            continue
        runs.append(value)
    return runs


def _doc_checks(root: Path) -> list:
    rows = []
    for path in _doc_paths(root):
        rows.extend(_doc_file_checks(path))
    return rows


def _doc_paths(root: Path) -> list:
    found = []
    if not root.is_dir():
        return found
    children = []
    try:
        children = sorted(root.iterdir())
    except OSError:
        children = []
    for name in _DOC_NAMES:
        path = root / name
        if path.is_file():
            found.append(path)
        for child in children:
            if not child.is_dir() or child.name in _SKIP_DIRS or child.name.startswith("."):
                continue
            nested = child / name
            if nested.is_file():
                found.append(nested)
    rules = root / ".cursor" / "rules"
    if rules.is_dir():
        found.extend(sorted(rules.glob("*.mdc")))
    return found


def _doc_file_checks(path: Path) -> list:
    heading = ""
    in_fence = False
    fence_ok = False
    rows = []
    for line in _read_capped(path).splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_fence:
                in_fence = False
                fence_ok = False
                continue
            lang = stripped[3:].strip().split()
            token = lang[0].lower() if lang else ""
            in_fence = True
            fence_ok = token in _FENCE_LANGS and _heading_matches(heading)
            continue
        if stripped.startswith("#") and not in_fence:
            heading = stripped.lstrip("#").strip()
            continue
        if not in_fence or not fence_ok or not stripped or stripped.startswith("#"):
            continue
        if not _line_has_runner(stripped):
            continue
        rows.append(PlannedCheck(
            cmd=stripped, role=ROLE_CHECK, source="docs",
            tier=TIER_AUTHORITATIVE,
            reason=f"from {path.name} › {heading}",
        ))
    return rows


def _heading_matches(title: str) -> bool:
    low = (title or "").lower()
    return any(mark in low for mark in _HEADING_MARKS)


def _line_has_runner(line: str) -> bool:
    argv = _argv(line)
    if not argv:
        return False
    names = [Path(token).name for token in argv]
    for index, name in enumerate(names):
        if name in _STRONG_RUNNERS:
            return True
        if index and (names[index - 1], name) in _RUNNER_PAIRS:
            return True
        if name in _AMBIGUOUS_RUNNERS and index:
            parent = names[index - 1]
            if parent in _RUNNER_PARENTS or parent == "-m":
                return True
    return False


def _memory_checks(learnings) -> list:
    if learnings is None:
        try:
            learnings = memory.get_learnings(limit=None)
        except OSError:
            learnings = []
    rows = []
    for row in learnings or []:
        if row.get("type") not in ("tool", "operational"):
            continue
        source = row.get("source") or "observed"
        tier = TIER_AUTHORITATIVE if source == "user-stated" else TIER_ADVISORY
        for _start, _end, cmd in _backtick_spans(row.get("insight") or ""):
            if _argv(cmd) is None:
                continue
            rows.append(PlannedCheck(
                cmd=cmd.strip(), role=ROLE_CHECK, source="memory", tier=tier,
                reason=f"from memory ({source})",
            ))
    return rows


def _history_checks(history) -> list:
    if history is None:
        history = _proven_history()
    rows = []
    for cmd in history or []:
        if _argv(cmd) is None:
            continue
        rows.append(PlannedCheck(
            cmd=cmd.strip(), role=ROLE_CHECK, source="history",
            tier=TIER_AUTHORITATIVE, reason="proven on an earlier run",
        ))
    return rows


def _proven_history() -> list:
    """Commands that went fail → pass in an earlier until-run, strongest first."""
    try:
        folder = memory.project_dir() / "until"
    except OSError:
        return []
    if not folder.is_dir():
        return []
    scores = {}
    for path in sorted(folder.glob("*.jsonl")):
        failed = set()
        meta_cmd = ""
        for ev in memory.read_jsonl(path):
            if ev.get("role") == "meta":
                meta_cmd = ev.get("check_cmd") or ""
            results = ev.get("results") if ev.get("role") in ("check", "baseline") else None
            if results:
                for row in results:
                    cmd = row.get("cmd") or ""
                    _note_transition(scores, failed, cmd, row.get("status"))
                continue
            if ev.get("role") == "check" and meta_cmd:
                _note_transition(scores, failed, meta_cmd, ev.get("status"))
    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    return [cmd for cmd, _score in ranked]


def _note_transition(scores: dict, failed: set, cmd: str, status: str) -> None:
    if not cmd:
        return
    if status == "fail":
        failed.add(cmd)
    elif status == "pass" and cmd in failed:
        scores[cmd] = scores.get(cmd, 0) + 1
        failed.discard(cmd)


def _resolves(argv0: str, root: Path) -> bool:
    if not argv0:
        return False
    path = Path(argv0)
    if argv0.startswith(("./", "../", "/")) or "/" in argv0:
        candidate = path if path.is_absolute() else root / path
        return candidate.exists()
    if shutil.which(argv0):
        return True
    name = path.name
    for marker in _PROJECT_MARKERS.get(name, ()):
        if (root / marker).exists():
            return True
    if (root / ".venv" / "bin" / name).exists():
        return True
    return False


def _first_file(root: Path, names: tuple):
    for name in names:
        path = root / name
        if path.is_file():
            return path
    return None


def _read_capped(path: Path) -> str:
    try:
        if path.stat().st_size > _DOC_LIMIT:
            return ""
        return path.read_text(errors="replace")
    except OSError:
        return ""


def _backtick_spans(text: str) -> list:
    spans = []
    i = 0
    while True:
        start = text.find("`", i)
        if start < 0:
            break
        end = text.find("`", start + 1)
        if end < 0:
            break
        spans.append((start, end + 1, text[start + 1:end]))
        i = end + 1
    return spans


def _quoted_spans(text: str) -> list:
    spans = []
    i = 0
    while i < len(text):
        if text[i] in "'\"":
            quote = text[i]
            end = text.find(quote, i + 1)
            if end < 0:
                break
            spans.append((i, end + 1, text[i + 1:end]))
            i = end + 1
            continue
        i += 1
    return spans


def _inside(pos: int, spans: list) -> bool:
    return any(start <= pos < end for start, end in spans)


def _blank(text: str, spans: list) -> str:
    chars = list(text)
    for start, end in spans:
        for index in range(start, min(end, len(chars))):
            chars[index] = " "
    return "".join(chars)


def _cue_role(window: str):
    low = window.lower()
    found = None
    found_at = -1
    for cue, role in _KEEP_CUES:
        idx = low.rfind(cue)
        if idx < 0 or idx < found_at:
            continue
        before = low[idx - 1] if idx else " "
        after_i = idx + len(cue)
        after = low[after_i] if after_i < len(low) else " "
        if before.isalnum() or after.isalnum():
            continue
        found_at = idx
        found = role
    return found


def command_parses(cmd: str) -> bool:
    """True when ShellCommand can tokenize ``cmd`` (rejects broken quotes)."""
    if _argv(cmd) is None:
        return False
    return bool(ShellCommand(cmd).argv)

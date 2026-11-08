"""Always-on steering markdown and a live clock for the system prompt.

Packaged, user, and project ``steer/*.md`` files are concatenated (later dirs
can contradict earlier). Unlike skills, same-name files are additive — they
are not an override. This module is a leaf: no imports of agent, tools, or loop.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from ...core.config import STATE_ROOT

STEER_DIR = Path(__file__).parent
USER_STEER_DIR = STATE_ROOT / "steer"

_SNAPSHOT_KEYS = (
    "utc",
    "local",
    "today",
    "7_days_ago",
    "28_days_ago",
    "90_days_ago",
)


def _invalid_steer_name(name: str) -> bool:
    """Reject empty, hidden, private, or path-like stems before reading."""
    return (
        not name
        or "/" in name
        or "\\" in name
        or name.startswith(".")
        or name.startswith("_")
        or ".." in name
    )


def steer_dirs(workspace_root: "Path | None" = None) -> "list[Path]":
    """Search order: packaged, ~/.lmloop/steer, <workspace>/.lmloop/steer."""
    dirs = [STEER_DIR, USER_STEER_DIR]
    if workspace_root is not None:
        dirs.append(Path(workspace_root) / ".lmloop" / "steer")
    return dirs


def load_steering(workspace_root: "Path | None" = None) -> str:
    """Concatenate steer markdown from existing dirs. Skip missing dirs."""
    parts = []
    for d in steer_dirs(workspace_root):
        if not d.is_dir():
            continue
        for path in sorted(d.glob("*.md")):
            if _invalid_steer_name(path.stem):
                continue
            try:
                text = path.read_text().strip()
            except OSError:
                continue
            if text:
                parts.append(text)
    return "\n\n".join(parts)


SANDBOX_PORT_HINT = (
    "Test servers must bind 0.0.0.0 inside the sandbox on a port in "
    "3000-3010 or 8000-8010; the host reaches them at 127.0.0.1."
)


def sandbox_port_hint() -> str:
    """Steering line appended only while a bridge sandbox is the active backend."""
    return SANDBOX_PORT_HINT


def steering_block(workspace_root: "Path | None" = None) -> str:
    """System-prompt section, or empty when no steer files load."""
    body = load_steering(workspace_root)
    if not body:
        return ""
    return "## Steering (always apply)\n\n" + body


# Repo instructions other agents already read. The first one present wins.
PROJECT_RULE_FILES = ("AGENTS.md", "CLAUDE.md")
PROJECT_RULES_LOCAL = Path(".lmloop") / "rules.md"
PROJECT_RULES_MAX_CHARS = 6000


def project_rule_paths(workspace_root: "Path | None") -> "list[Path]":
    """``AGENTS.md`` (else ``CLAUDE.md``) and ``.lmloop/rules.md`` that exist."""
    if workspace_root is None:
        return []
    root = Path(workspace_root)
    found = [root / name for name in PROJECT_RULE_FILES if (root / name).is_file()][:1]
    if (root / PROJECT_RULES_LOCAL).is_file():
        found.append(root / PROJECT_RULES_LOCAL)
    return found


def project_rules_block(workspace_root: "Path | None") -> str:
    """System-prompt section with the workspace's own agent rules, clipped."""
    parts, names = [], []
    for path in project_rule_paths(workspace_root):
        try:
            text = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            continue
        if text:
            parts.append(text)
            names.append(path.relative_to(workspace_root).as_posix())
    if not parts:
        return ""
    body = "\n\n".join(parts)
    if len(body) > PROJECT_RULES_MAX_CHARS:
        body = (body[:PROJECT_RULES_MAX_CHARS].rstrip()
                + f"\n\n[clipped at {PROJECT_RULES_MAX_CHARS} chars — read_file the source for the rest]")
    return f"## Project rules (from {', '.join(names)})\n\n{body}"


def clock_snapshot(now: "datetime | None" = None) -> dict:
    """UTC clock plus lookback calendar dates. ``now`` is for tests."""
    if now is None:
        utc = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        utc = now.replace(tzinfo=timezone.utc)
    else:
        utc = now.astimezone(timezone.utc)
    local = utc.astimezone()
    today = utc.date()
    return {
        "utc": utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "local": local.isoformat(timespec="seconds"),
        "today": today.isoformat(),
        "7_days_ago": (today - timedelta(days=7)).isoformat(),
        "28_days_ago": (today - timedelta(days=28)).isoformat(),
        "90_days_ago": (today - timedelta(days=90)).isoformat(),
    }


def clock_block(now: "datetime | None" = None) -> str:
    """System-prompt clock. Callers pass a frozen ``now`` for a session or run."""
    snap = clock_snapshot(now)
    return (
        "## Clock\n\n"
        f"Current UTC time: {snap['utc']}\n"
        f"Today's date: {snap['today']}\n"
        "This clock is the source of truth for \"now\". "
        "Do not use a training-cutoff year for relative windows "
        "(\"last 4 weeks\", \"today\", \"this year\"). "
        "If this session is hours old, call `current_time` and use its dates."
    )


def format_current_time() -> str:
    """Tool body: ISO timestamps and lookback dates. No network."""
    snap = clock_snapshot()
    lines = [f"{key}: {snap[key]}" for key in _SNAPSHOT_KEYS]
    lines.append(
        "Use these YYYY-MM-DD values in web_search queries. "
        "Do not use a training-cutoff year."
    )
    return "\n".join(lines)

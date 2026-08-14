"""Always-on steering markdown and a live clock for the system prompt.

Packaged, user, and project ``steer/*.md`` files are concatenated (later dirs
can contradict earlier). Unlike skills, same-name files are additive — they
are not an override. ``steer.py`` is a leaf: no imports of agent, tools, or loop.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import STATE_ROOT

STEER_DIR = Path(__file__).parent / "steer"
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


def steering_block(workspace_root: "Path | None" = None) -> str:
    """System-prompt section, or empty when no steer files load."""
    body = load_steering(workspace_root)
    if not body:
        return ""
    return "## Steering (always apply)\n\n" + body


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
    """System-prompt clock. Rebuilt every ``system_prompt()`` call."""
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

"""Config + paths + project namespace for lmloop.

State layout (gstack-inspired, file-only):

    ~/.lmloop/
    ├── config.json                    # user settings
    ├── history                        # REPL prompt history (prompt_toolkit)
    ├── skills/<name>.md               # user-authored skills (override packaged)
    └── projects/<slug>/
        ├── learnings.jsonl            # append-only, dedup at read time
        ├── decisions.jsonl            # event-sourced (decide / supersede)
        ├── sessions/<ts>.jsonl        # per-session transcript history
        ├── checkpoints/<ts>-<t>.md    # context-save handoffs
        └── until/<ts>.jsonl           # goal-loop run log (maker/check/eval)
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

STATE_ROOT = Path(os.environ.get("LMLOOP_HOME", str(Path.home() / ".lmloop")))
CONFIG_PATH = STATE_ROOT / "config.json"

DEFAULTS = {
    "base_url": "http://127.0.0.1:1234/v1",
    "model": "",  # empty = first model the server reports
    "max_rounds": 60,
    "max_continue_nudges": 2,  # auto-resume when model narrates next step without tools
    "temperature": 0.7,
    "timeout_s": 600,
    "stream": True,  # stream tokens as they arrive (SSE); false = wait for full reply
    "context_learnings": 8,
    "context_decisions": 6,
    "confirm_shell": True,  # master: False disables all shell confirms
    "confirm_destructive": True,
    "confirm_shell_syntax": False,  # pipes/redirections; off to avoid fatigue
    "shell_timeout_s": 120,
    "web_timeout_s": 30,
    "max_tool_output": 12000,
    "auto_start_server": True,
    "color": True,
    "context_length": 0,  # 0 = auto-detect from LM Studio; manual override in tokens
    "context_reserve": 2048,  # tokens reserved for model reply when showing fill bar
    "until_max_steps": 12,  # maker cycles per until invocation before pause
    "until_mine": True,  # after until pass, mine learnings from the run
}

_CONFIG_WARNED = False
_BOOL_TRUE = frozenset({"1", "true", "yes", "y"})
_BOOL_FALSE = frozenset({"0", "false", "no", "n"})


def coerce_config_value(key: str, value):
    """Coerce a config value to the DEFAULTS type for ``key``.

    Returns the coerced value, or None if the value is invalid (caller falls
    back to DEFAULTS). Unknown keys return None.
    """
    if key not in DEFAULTS:
        return None
    default = DEFAULTS[key]
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            low = value.strip().lower()
            if low in _BOOL_TRUE:
                return True
            if low in _BOOL_FALSE:
                return False
        return None
    if isinstance(default, int) and not isinstance(default, bool):
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            try:
                return int(value.strip())
            except ValueError:
                return None
        return None
    if isinstance(default, float):
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                return None
        return None
    if isinstance(value, str):
        return value
    return None


def normalize_config(raw: dict) -> dict:
    """Keep known keys, coerce types, drop unknown keys. Invalid values omitted."""
    out = {}
    for key in DEFAULTS:
        if key not in raw:
            continue
        coerced = coerce_config_value(key, raw[key])
        if coerced is None:
            continue
        out[key] = coerced
    return out


def load_config() -> dict:
    global _CONFIG_WARNED
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            raw = json.loads(CONFIG_PATH.read_text())
            if isinstance(raw, dict):
                known = {k: raw[k] for k in DEFAULTS if k in raw}
                invalid = [
                    k for k, v in known.items()
                    if coerce_config_value(k, v) is None
                ]
                cfg.update(normalize_config(raw))
                if invalid and not _CONFIG_WARNED:
                    print(
                        f"[lmloop] warning: {CONFIG_PATH} has invalid values for "
                        f"{', '.join(invalid)}; using defaults for those keys",
                        file=sys.stderr,
                    )
                    _CONFIG_WARNED = True
            elif not _CONFIG_WARNED:
                print(
                    f"[lmloop] warning: {CONFIG_PATH} is not a JSON object; using defaults",
                    file=sys.stderr,
                )
                _CONFIG_WARNED = True
        except (json.JSONDecodeError, OSError) as e:
            if not _CONFIG_WARNED:
                print(
                    f"[lmloop] warning: could not load {CONFIG_PATH} ({e}); using defaults",
                    file=sys.stderr,
                )
                _CONFIG_WARNED = True
    return cfg


def save_config(cfg: dict) -> None:
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    known = {k: v for k, v in cfg.items() if k in DEFAULTS}
    CONFIG_PATH.write_text(json.dumps(known, indent=2) + "\n")


def project_slug(cwd: "Path | None" = None) -> str:
    """Namespace memory per project: git remote owner/repo if available, else dir name."""
    cwd = cwd or Path.cwd()
    try:
        url = subprocess.run(
            ["git", "-C", str(cwd), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        url = ""
    if url:
        # git@host:owner/repo.git or https://host/owner/repo
        m = re.search(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?/?$", url)
        if m:
            raw = f"{m.group(1)}-{m.group(2)}"
        else:
            raw = cwd.name
    else:
        # non-git dir, or git repo without a remote: use the repo/dir name
        try:
            top = subprocess.run(
                ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            raw = Path(top).name if top else cwd.name
        except (OSError, subprocess.TimeoutExpired):
            raw = cwd.name
    return re.sub(r"[^a-zA-Z0-9._-]", "-", raw) or "default"


def project_dir(slug: "str | None" = None) -> Path:
    d = STATE_ROOT / "projects" / (slug or project_slug())
    d.mkdir(parents=True, exist_ok=True)
    return d

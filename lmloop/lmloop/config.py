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
        └── checkpoints/<ts>-<t>.md    # context-save handoffs
"""

import json
import os
import re
import subprocess
from pathlib import Path

STATE_ROOT = Path(os.environ.get("LMLOOP_HOME", str(Path.home() / ".lmloop")))
CONFIG_PATH = STATE_ROOT / "config.json"

DEFAULTS = {
    "base_url": "http://127.0.0.1:1234/v1",
    "model": "",  # empty = first model the server reports
    "max_rounds": 25,
    "max_continue_nudges": 2,  # auto-resume when model narrates next step without tools
    "temperature": 0.7,
    "timeout_s": 600,
    "stream": True,  # stream tokens as they arrive (SSE); false = wait for full reply
    "context_learnings": 8,
    "context_decisions": 6,
    "confirm_shell": True,
    "auto_start_server": True,
    "color": True,
    "context_length": 0,  # 0 = auto-detect from LM Studio; manual override in tokens
    "context_reserve": 2048,  # tokens reserved for model reply when showing fill bar
}


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text()))
        except (json.JSONDecodeError, OSError):
            pass
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

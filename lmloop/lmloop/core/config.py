"""Config + paths + project namespace for lmloop.

State layout (gstack-inspired, file-only):

    ~/.lmloop/
    ├── config.json                    # user settings
    ├── usage.jsonl                    # append-only local feature-usage events
    ├── traces.jsonl                   # local parent/child spans (LMLOOP_TRACE=0 off)
    ├── eval_queue.jsonl               # spans waiting for `lmloop eval --drain`
    ├── evals/last_report.json         # last `lmloop eval` report (gaps + flow)
    ├── evals/golden_inbox.jsonl       # anonymized span failures for review
    ├── history                        # REPL prompt history (prompt_toolkit)
    ├── skills/<name>.md               # user-authored skills (override packaged)
    ├── graphs/<name>.md               # user-authored graphs (override packaged)
    ├── steer/<name>.md                # user always-on steering (concatenated)
    └── projects/<slug>/
        ├── learnings.jsonl            # append-only, dedup at read time
        ├── decisions.jsonl            # event-sourced (decide / supersede)
        ├── sessions/<ts>.jsonl        # per-session transcript history
        ├── checkpoints/<ts>-<t>.md    # context-save handoffs
        ├── until/<ts>.jsonl           # goal-loop run log (maker/check/eval)
        ├── graphs/<name>/<ts>.jsonl   # workflow-graph run log
        ├── graph_nodes.jsonl          # knowledge-graph nodes (use_graph)
        └── graph_edges.jsonl          # knowledge-graph edges (use_graph)
"""

import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

STATE_ROOT = Path(os.environ.get("LMLOOP_HOME", str(Path.home() / ".lmloop")))
CONFIG_PATH = STATE_ROOT / "config.json"


def utc_now() -> str:
    """UTC timestamp as ``YYYY-MM-DDTHH:MM:SSZ``."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Shared spelling for DEFAULTS and every reader of these keys.
TOOL_PREVIEW_LIMIT = "tool_preview_limit"
DIFF_LIMIT = "diff_limit"
CHECK_OUTPUT_LIMIT = "check_output_limit"
HISTORY_PREVIEW_CHARS = "history_preview_chars"
MAX_TRANSCRIPT_TOOL_CHARS = "max_transcript_tool_chars"
WEB_SEARCH_CONCURRENCY = "web_search_concurrency"
WEB_FETCH_CONCURRENCY = "web_fetch_concurrency"
LOCAL_READ_CONCURRENCY = "local_read_concurrency"


@dataclass
class ConfigSchema:
    """Typed defaults for every persisted ``config.json`` key (stdlib, not Pydantic)."""

    base_url: str = "http://127.0.0.1:1234/v1"
    api_key: str = ""  # empty = no Authorization header; see resolve_api_key()
    model: str = ""  # empty = first model the server reports
    max_rounds: int = 60
    max_continue_nudges: int = 2  # auto-resume when model narrates next step without tools
    temperature: float = 0.7
    timeout_s: int = 600
    stream: bool = True  # stream tokens as they arrive (SSE); false = wait for full reply
    context_learnings: int = 8
    context_decisions: int = 6
    confirm_shell: bool = True  # master: False disables all shell confirms
    confirm_destructive: bool = True
    confirm_shell_syntax: bool = False  # pipes/redirections; off to avoid fatigue
    autonomous_gates: str = "files"  # files | none | all — gate policy for until/graph
    autonomous_snapshot: Literal["git", "off"] = "git"
    shell_timeout_s: int = 120
    web_timeout_s: int = 30
    max_tool_output: int = 12000
    auto_start_server: bool = True
    color: bool = True
    context_length: int = 0  # 0 = auto-detect from LM Studio; manual override in tokens
    context_reserve: int = 2048  # tokens reserved for model reply when showing fill bar
    eval_max_rounds: int = 8  # gather rounds for until/graph eval act(); maker keeps max_rounds
    until_max_steps: int = 12  # maker cycles per until invocation before pause
    until_control: Literal["off", "budget", "meta"] = "off"
    until_mine: bool = True  # after until pass, mine learnings from the run
    mine_on_exit: bool = False  # REPL exit mines the current session when true
    check_inference: Literal["auto", "off"] = "auto"
    until_baseline: Literal["auto", "off"] = "auto"
    sandbox_image: str = ""  # digest-pinned image; inert unless --docker
    sandbox_network: Literal["bridge", "none", "host"] = "bridge"
    model_concurrency: str | int = "auto"  # auto: 1 local / 4 remote; or int 1..8
    run_token_budget: int = 0  # pause run after this many tokens; 0 = off
    eval_model: str = ""  # checker / proposals / mining; empty = main model
    memory_index: Literal["auto", "off", "on"] = "auto"
    recall_sessions: Literal["auto", "off", "on"] = "auto"
    graph_max_steps: int = 24  # node entries per graph invocation before pause
    graph_mine: bool = True  # after a terminal graph pass (no mine node), mine learnings
    use_graph: bool = False  # opt-in knowledge-graph memory (JSONL nodes/edges)
    vision: str = "auto"  # auto | true | false — LM Studio VLM flag override
    company_remote: bool = False
    company_models_allowlist: str = ""
    use_spirit: bool = True
    spirit_actions: bool = True
    spirit_distill_after_mine: bool = False
    spirit_remote: bool = False
    campaign_daily_reflect: bool = True
    campaign_end_of_day_utc: str = ""  # empty = off; else hour 0-23 UTC
    campaign_max_days: int = 30
    tool_preview_limit: int = 160
    diff_limit: int = 60
    check_output_limit: int = 2000
    history_preview_chars: int = 120
    max_transcript_tool_chars: int = 0  # 0 = no limit for /transcript tool rows
    web_search_concurrency: int = 2
    web_fetch_concurrency: int = 4
    local_read_concurrency: int = 8


DEFAULTS: dict[str, object] = asdict(ConfigSchema())

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
    if key in ("check_inference", "until_baseline"):
        if isinstance(value, str) and value.strip().lower() in ("auto", "off"):
            return value.strip().lower()
        return None
    if key == "until_control":
        if isinstance(value, str) and value.strip().lower() in ("off", "budget", "meta"):
            return value.strip().lower()
        return None
    if key in ("memory_index", "recall_sessions"):
        if isinstance(value, str) and value.strip().lower() in ("auto", "off", "on"):
            return value.strip().lower()
        return None
    if key == "model_concurrency":
        if isinstance(value, str) and value.strip().lower() == "auto":
            return "auto"
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return max(1, min(8, value))
        if isinstance(value, str):
            try:
                return max(1, min(8, int(value.strip())))
            except ValueError:
                return None
        return None
    if key == "run_token_budget":
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return max(0, value)
        if isinstance(value, str):
            try:
                return max(0, int(value.strip()))
            except ValueError:
                return None
        return None
    if key == "campaign_end_of_day_utc":
        if isinstance(value, str):
            low = value.strip()
            if low == "":
                return ""
            if low.isdigit() and 0 <= int(low) <= 23:
                return str(int(low))
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 23:
            return str(value)
        return None
    if key == "autonomous_snapshot":
        if isinstance(value, str):
            low = value.strip().lower()
            if low in ("git", "off"):
                return low
        return None
    if key == "sandbox_network":
        if isinstance(value, str) and value.strip().lower() in ("bridge", "none", "host"):
            return value.strip().lower()
        return None
    if key == "sandbox_image":
        if not isinstance(value, str):
            return None
        text = value.strip()
        if text and "@sha256:" not in text:
            return None
        return text
    if key == "vision":
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, str):
            low = value.strip().lower()
            if low == "auto":
                return "auto"
            if low in _BOOL_TRUE:
                return "true"
            if low in _BOOL_FALSE:
                return "false"
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


class Config(dict):
    """User settings. A dict, so ``cfg[key]``, ``.get``, and ``.update`` keep working.

    Attribute reads use the same keys: ``cfg.max_rounds``. A name that is not a
    key raises AttributeError. Extra keys, such as company runtime flags, are
    set with ``cfg[name] = value``.
    """

    def __init__(self, values: Mapping | None = None):
        super().__init__(values or {})

    def copy(self) -> "Config":
        return Config(self)

    def __getattr__(self, name: str):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None


def cfg_get(cfg: Config | None, key: str):
    """Return ``cfg[key]`` when the key is present, else ``DEFAULTS[key]``.

    Prefer this over ``cfg.get(key) or default`` so legitimate zero values are
    not replaced by the default.
    """
    if not cfg or key not in cfg:
        return DEFAULTS[key]
    return cfg[key]


def cfg_int(cfg: Config | None, key: str) -> int:
    return int(cfg_get(cfg, key))


def cfg_positive_int(cfg: Config | None, key: str) -> int:
    """``cfg_int`` when it is at least 1; otherwise ``DEFAULTS[key]``."""
    val = cfg_int(cfg, key)
    if val < 1:
        return int(DEFAULTS[key])
    return val


def cfg_bool(cfg: Config | None, key: str) -> bool:
    return bool(cfg_get(cfg, key))


def cfg_float(cfg: Config | None, key: str) -> float:
    return float(cfg_get(cfg, key))


def cfg_str(cfg: Config | None, key: str) -> str:
    return str(cfg_get(cfg, key))


def config_schema(cfg: Config | None = None) -> ConfigSchema:
    """Resolve every known key through ``cfg_get`` (partial ``cfg`` fills the rest)."""
    return ConfigSchema(**{f.name: cfg_get(cfg, f.name) for f in fields(ConfigSchema)})


def resolve_api_key(cfg: Config) -> str:
    """Bearer token for OpenAI-compatible APIs (OpenRouter, etc.)."""
    key = cfg_str(cfg, "api_key").strip()
    if key:
        return key
    for env_name in ("OPENROUTER_API_KEY", "LMLOOP_API_KEY"):
        val = os.environ.get(env_name, "").strip()
        if val:
            return val
    return ""


def api_auth_headers(cfg: Config) -> dict:
    key = resolve_api_key(cfg)
    if not key:
        return {}
    return {"Authorization": f"Bearer {key}"}


def openrouter_extra_headers() -> dict:
    """Optional OpenRouter ranking headers from the environment."""
    out = {}
    referer = os.environ.get("OPENROUTER_HTTP_REFERER", "").strip()
    title = os.environ.get("OPENROUTER_X_TITLE", "").strip()
    if referer:
        out["HTTP-Referer"] = referer
    if title:
        out["X-Title"] = title
    return out


def chat_request_headers(cfg: Config, *, stream: bool) -> dict:
    headers = {"Content-Type": "application/json"}
    headers.update(api_auth_headers(cfg))
    headers.update(openrouter_extra_headers())
    headers["Accept"] = "text/event-stream" if stream else "application/json"
    return headers


def load_config() -> Config:
    global _CONFIG_WARNED
    cfg = Config(DEFAULTS)
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


def save_config(cfg: Config) -> None:
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

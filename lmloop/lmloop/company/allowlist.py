"""OpenRouter model allowlist for company mode. Leaf: no chat, loop, or graph."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from ..config import cfg_bool, cfg_str

PACKAGED_ALLOWLIST = (
    Path(__file__).resolve().parents[2] / "company" / "openrouter_autonomous.yaml"
)
RUNTIME_ACTIVE = "company_active"
RUNTIME_ALLOWLIST = "company_allowlist"
RUNTIME_ORCHESTRATOR = "company_orchestrator"


def packaged_models(text: "str | None" = None, path: "Path | None" = None) -> list[str]:
    """Model ids from the packaged autonomy file (``- vendor/model`` lines)."""
    if text is None:
        file = path or PACKAGED_ALLOWLIST
        text = file.read_text() if file.is_file() else ""
    found: list[str] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("- ") or line.startswith("#"):
            continue
        item = line[2:].strip()
        if "/" not in item or " " in item or item in seen:
            continue
        seen.add(item)
        found.append(item)
    return found


def extra_models(cfg: dict) -> list[str]:
    """Comma-separated ``company_models_allowlist`` additions."""
    raw = cfg_str(cfg, "company_models_allowlist")
    found: list[str] = []
    seen: set[str] = set()
    for part in raw.split(","):
        item = part.strip()
        if not item or item in seen:
            continue
        seen.add(item)
        found.append(item)
    return found


def effective_allowlist(cfg: dict) -> list[str]:
    """Packaged ids union user extensions. Order is stable; ids are unique."""
    found: list[str] = []
    seen: set[str] = set()
    for item in packaged_models() + extra_models(cfg):
        if item in seen:
            continue
        seen.add(item)
        found.append(item)
    return found


def base_url_host(cfg: dict) -> str:
    return (urlparse(cfg_str(cfg, "base_url")).hostname or "").lower()


def endpoint_error(cfg: dict) -> "str | None":
    """None when this base_url may run company mode."""
    from ..server import base_url_is_local

    host = base_url_host(cfg)
    remote_ok = cfg_bool(cfg, "company_remote")
    if base_url_is_local(cfg) and not remote_ok:
        return "company mode needs a remote base_url, or company_remote true"
    if "openrouter.ai" not in host and not remote_ok:
        return (
            "company mode requires an OpenRouter base_url, "
            "or company_remote true for another aggregator"
        )
    return None


def activate(cfg: dict, *, orchestrator: bool) -> dict:
    """Return a cfg copy that enforces the allowlist on every chat call."""
    out = dict(cfg)
    out[RUNTIME_ACTIVE] = True
    out[RUNTIME_ORCHESTRATOR] = bool(orchestrator)
    out[RUNTIME_ALLOWLIST] = effective_allowlist(cfg)
    if not orchestrator:
        out["autonomous_gates"] = "all"
    return out


def rejection(cfg: dict, model: str) -> "str | None":
    """Error string when company mode is on and ``model`` is not allowlisted."""
    if not cfg.get(RUNTIME_ACTIVE):
        return None
    allow = cfg.get(RUNTIME_ALLOWLIST) or []
    if model in allow:
        return None
    return f"model {model!r} is not on the company allowlist"


def worker_memory_blocked(cfg: dict) -> bool:
    """Workers do not write project memory. The orchestrator does, after gates."""
    return bool(cfg.get(RUNTIME_ACTIVE)) and not bool(cfg.get(RUNTIME_ORCHESTRATOR))

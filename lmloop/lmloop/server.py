"""LM Studio / OpenAI-compatible server bring-up and model metadata.

Owns ``lms`` auto-start, model listing, context-window discovery, and VLM
detection. Chat HTTP stays in ``agent.py``.
"""

import json
import shutil
import subprocess
import time
import urllib.request

from . import status as status_mod

API_MODELS_PATH = "/models"
NATIVE_MODELS_PATHS = ("/api/v0/models", "/api/v1/models")


class ServerError(RuntimeError):
    pass


def _get_json(url: str, timeout: int = 5) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _get(base_url: str, path: str, timeout: int = 5):
    return _get_json(base_url.rstrip("/") + path, timeout)


def _native_base(base_url: str) -> str:
    url = base_url.rstrip("/")
    if url.endswith("/v1"):
        return url[:-3]
    return url


def _model_matches(entry: dict, model: str) -> bool:
    """Prefer exact id/key match; allow suffix match only for multi-segment ids."""
    ids = [entry.get("id", ""), entry.get("key", ""), entry.get("display_name", "")]
    ids = [i for i in ids if i]
    if model in ids:
        return True
    # Avoid bare endswith on short fragments (wrong-model context window).
    if "/" in model or len(model) >= 8:
        return any(model == i or i.endswith("/" + model) or model.endswith("/" + i)
                   for i in ids)
    return False


def _native_model_entries(cfg: dict) -> list:
    """LM Studio native model records, or empty if the API is unreachable."""
    base_url = cfg.get("base_url") or ""
    if not base_url:
        return []
    base = _native_base(base_url)
    for path in NATIVE_MODELS_PATHS:
        try:
            data = _get_json(base + path)
        except (OSError, json.JSONDecodeError):
            continue
        models = data.get("data") or data.get("models") or []
        if models:
            return models
    return []


def _entry_has_vision(entry: dict) -> bool:
    """True when a native models API record looks like a VLM."""
    kind = str(entry.get("type") or entry.get("model_type") or "").lower()
    if kind in ("vlm", "vision"):
        return True
    if entry.get("vision") is True:
        return True
    caps = entry.get("capabilities")
    if isinstance(caps, dict) and caps.get("vision"):
        return True
    if isinstance(caps, (list, tuple)):
        if any(str(c).lower() in ("vision", "vlm") for c in caps):
            return True
    for inst in entry.get("loaded_instances") or []:
        inst_cfg = inst.get("config") or {}
        if inst_cfg.get("vision") is True:
            return True
        if str(inst_cfg.get("type") or "").lower() in ("vlm", "vision"):
            return True
    return False


def _run_lms(args: list, echo=print, timeout: int = 60) -> bool:
    """Run an lms CLI command; echo stderr on failure. Returns True on success."""
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        echo(f"lms failed: {e}")
        return False
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        echo(f"lms {' '.join(args)} failed (exit {proc.returncode})"
             + (f": {err[:500]}" if err else ""))
        return False
    return True


class LmsClient:
    """One OpenAI-compatible server: models, context length, vision, bring-up.

    Vision detection is cached on the class so repeated ``act()`` rounds do not
    re-query the native models API.
    """

    _vision_cache: "dict[tuple, bool]" = {}

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def list_models(self) -> "list[str]":
        return list_models(self.cfg.get("base_url") or "")

    def native_entries(self) -> list:
        return _native_model_entries(self.cfg)

    def context_limit(self, model: str) -> int:
        """Return loaded context window size in tokens. 0 if unknown."""
        manual = int(self.cfg.get("context_length") or 0)
        if manual > 0:
            return manual
        for entry in self.native_entries():
            if not _model_matches(entry, model):
                continue
            for inst in entry.get("loaded_instances") or []:
                loaded = (inst.get("config") or {}).get("context_length")
                if loaded:
                    return int(loaded)
            max_ctx = entry.get("max_context_length")
            if max_ctx:
                return int(max_ctx)
        return 0

    def has_vision(self, model: str) -> bool:
        """Whether to attach image_url parts. Config vision: auto|true|false."""
        mode = str(self.cfg.get("vision") or "auto").strip().lower()
        if mode == "true":
            return True
        if mode == "false":
            return False
        key = (self.cfg.get("base_url") or "", model or "")
        if key in type(self)._vision_cache:
            return type(self)._vision_cache[key]
        found = False
        for entry in self.native_entries():
            if _model_matches(entry, model):
                found = _entry_has_vision(entry)
                break
        type(self)._vision_cache[key] = found
        return found

    def ensure(self, echo=print) -> str:
        """Make sure the server is reachable and a model is loaded. Returns model id."""
        base = self.cfg["base_url"]
        models = list_models(base)
        if not models and self.cfg.get("auto_start_server") and shutil.which("lms"):
            echo(status_mod.msg_starting_server())
            _run_lms(["lms", "server", "start"], echo=echo, timeout=60)
            for _ in range(20):
                time.sleep(1)
                models = list_models(base)
                if models:
                    break
        if not models:
            # server may be up with nothing loaded — try loading the configured model
            if shutil.which("lms"):
                want = self.cfg.get("model") or ""
                echo(status_mod.msg_loading_model(want))
                args = ["lms", "load", "--yes"] + ([want] if want else [])
                _run_lms(args, echo=echo, timeout=300)
                models = list_models(base)
        if not models:
            raise ServerError(status_mod.msg_no_models(base))
        want = self.cfg.get("model")
        if want and want in models:
            return want
        if want:
            echo(status_mod.msg_model_fallback(want, models[0]))
        return models[0]


def list_models(base_url: str) -> "list[str]":
    try:
        data = _get(base_url, API_MODELS_PATH)
        return [m["id"] for m in data.get("data", [])]
    except (OSError, json.JSONDecodeError, KeyError):
        return []


def get_context_limit(model: str, cfg: dict) -> int:
    return LmsClient(cfg).context_limit(model)


def model_has_vision(model: str, cfg: dict) -> bool:
    return LmsClient(cfg).has_vision(model)


def ensure_server(cfg: dict, echo=print) -> str:
    return LmsClient(cfg).ensure(echo=echo)

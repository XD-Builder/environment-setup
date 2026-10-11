"""OpenAI-compatible chat HTTP. Leaf: urllib + SSE ingest + status copy.

``act()`` and ``generate_skill_draft()`` call ``_chat``. Server bring-up and
model metadata stay in ``server.py``. This module must not import agent,
tools, loop, or graph.
"""

import json
import urllib.error
import urllib.request

from . import server, status as status_mod
from .config import cfg_bool, cfg_float, cfg_int, chat_request_headers
from .stream import StreamError, _read_sse

API_CHAT_PATH = "/chat/completions"


def _chat_request(cfg: dict, model: str, messages: list, tool_specs: "list | None",
                  stream: bool) -> urllib.request.Request:
    payload = {
        "model": model,
        "messages": status_mod.api_messages(messages),
        "temperature": cfg_float(cfg, "temperature"),
        "stream": stream,
    }
    if stream:
        # Ask the server for a final usage chunk (OpenAI-compatible; ignored if unsupported).
        payload["stream_options"] = {"include_usage": True}
    if tool_specs:
        payload["tools"] = tool_specs
        payload["tool_choice"] = "auto"
    return urllib.request.Request(
        cfg["base_url"].rstrip("/") + API_CHAT_PATH,
        data=json.dumps(payload).encode(),
        headers=chat_request_headers(cfg, stream=stream),
    )


def _raise_http_error(e: urllib.error.HTTPError, cfg: dict, body: "str | None" = None) -> None:
    if body is None:
        try:
            body = e.read().decode("utf-8", errors="replace")[:800]
        except OSError:
            body = ""
    lower = (body or "").lower()
    if e.code in (400, 413) and any(
        t in lower for t in ("context", "token", "length", "too long", "maximum")
    ):
        raise server.ServerError(status_mod.msg_context_overflow()) from e
    raise server.ServerError(f"LM Studio returned HTTP {e.code}: {(body or '')[:500]}") from e


def _chat_once(cfg: dict, model: str, messages: list, tool_specs: "list | None") -> "tuple[dict, dict]":
    """Non-streaming chat completion."""
    req = _chat_request(cfg, model, messages, tool_specs, stream=False)
    slots = server.model_slots(cfg)

    def _wait(limit: int) -> None:
        print(f"[waiting for model slot · {limit}/{limit} busy]")

    try:
        with slots.acquire(on_wait=_wait):
            with urllib.request.urlopen(req, timeout=cfg_int(cfg, "timeout_s")) as resp:
                data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        _raise_http_error(e, cfg)
    except OSError as e:
        raise server.ServerError(f"Cannot reach LM Studio at {cfg['base_url']}: {e}")
    try:
        usage = data.get("usage") or {}
        return data["choices"][0]["message"], usage
    except (KeyError, IndexError):
        raise server.ServerError(f"Unexpected response shape: {json.dumps(data)[:500]}")


def _chat_stream(cfg: dict, model: str, messages: list, tool_specs: "list | None",
                 on_delta=None, on_activity=None, on_reasoning=None,
                 on_tools=None) -> "tuple[dict, dict]":
    """Streaming chat completion (SSE). Assembles message; optionally echoes content deltas."""
    req = _chat_request(cfg, model, messages, tool_specs, stream=True)
    slots = server.model_slots(cfg)

    def _wait(limit: int) -> None:
        print(f"[waiting for model slot · {limit}/{limit} busy]")

    def _open_and_read(request):
        with slots.acquire(on_wait=_wait):
            with urllib.request.urlopen(request, timeout=cfg_int(cfg, "timeout_s")) as resp:
                return _read_sse(
                    resp, on_delta=on_delta, on_activity=on_activity,
                    on_reasoning=on_reasoning, on_tools=on_tools,
                )

    try:
        return _open_and_read(req)
    except StreamError as e:
        raise server.ServerError(str(e)) from e
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")[:800]
        except OSError:
            body = ""
        payload = json.loads(req.data.decode())
        # Older / stricter servers may reject stream_options — retry without it.
        if e.code == 400 and "stream_options" in payload:
            payload.pop("stream_options", None)
            retry = urllib.request.Request(
                req.full_url,
                data=json.dumps(payload).encode(),
                headers=chat_request_headers(cfg, stream=True),
            )
            try:
                return _open_and_read(retry)
            except StreamError as e2:
                raise server.ServerError(str(e2)) from e2
            except urllib.error.HTTPError as e2:
                _raise_http_error(e2, cfg)
            except OSError as e2:
                raise server.ServerError(f"Cannot reach LM Studio at {cfg['base_url']}: {e2}")
        _raise_http_error(e, cfg, body=body)
    except OSError as e:
        raise server.ServerError(f"Cannot reach LM Studio at {cfg['base_url']}: {e}")


def _chat(cfg: dict, model: str, messages: list, tool_specs: "list | None",
          on_delta=None, on_activity=None, on_reasoning=None,
          on_tools=None) -> "tuple[dict, dict]":
    """Chat completion. Streams when cfg['stream'] is true (default)."""
    from .company.allowlist import rejection
    reason = rejection(cfg, model)
    if reason:
        raise server.ServerError(reason)
    if cfg_bool(cfg, "stream"):
        return _chat_stream(
            cfg, model, messages, tool_specs,
            on_delta=on_delta, on_activity=on_activity,
            on_reasoning=on_reasoning, on_tools=on_tools,
        )
    return _chat_once(cfg, model, messages, tool_specs)

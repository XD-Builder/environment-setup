"""Owned status / resume copy — single source of truth for agent + REPL UX."""

STATUS_PREFIX = "[lmloop]"
MSG_RESUME = "type /continue to resume"

# Message metadata key for internal control messages (stripped before API send).
LMLOOP_META = "_lmloop"
META_NUDGE = "nudge"

NUDGE_CONTINUE_TEXT = (
    f"{STATUS_PREFIX} You described a next step but did not call any tools and did not "
    "finish the user's request. Continue now: either call the tools you need, "
    "or give the final answer."
)


def status(msg: str) -> str:
    """Format an lmloop status line for echo_status."""
    msg = (msg or "").strip()
    if msg.startswith(STATUS_PREFIX):
        return msg
    return f"{STATUS_PREFIX} {msg}"


def msg_paused_continuing() -> str:
    return status("model paused mid-task — continuing…")


def msg_stopped_unfinished() -> str:
    return status(f"model stopped without finishing — {MSG_RESUME}")


def msg_hit_max_rounds() -> str:
    return status(f"hit max_rounds — stopping. {MSG_RESUME}")


def msg_until_step(role: str, step: int, max_steps: int) -> str:
    return status(f"until {role} · cycle {step}/{max_steps}")


def msg_until_paused() -> str:
    return status(f"until hit until_max_steps — stopping. {MSG_RESUME}")


def msg_until_blocked() -> str:
    return status("until checker is blocked — need a yes/no")


def msg_until_mining() -> str:
    return status("until passed — mining learnings")


def msg_until_done() -> str:
    return status("until: goal met")


def msg_until_continue() -> str:
    return status("until done — type to continue")


def msg_context_pressure(pct: int) -> str:
    return status(
        f"context ~{pct}% full — /compact or /save recommended before continuing"
    )


def msg_context_overflow() -> str:
    return status(
        f"context window overflow from the server — /compact or /new, then {MSG_RESUME}"
    )


def msg_starting_server() -> str:
    return status("LM Studio server not reachable — starting it with `lms server start`...")


def msg_loading_model(want: str) -> str:
    label = want or "(first available)"
    return status(f"No model loaded — running `lms load {label}`...")


def msg_model_fallback(want: str, used: str) -> str:
    return status(f"configured model '{want}' is not loaded; using '{used}'")


def msg_no_models(base: str) -> str:
    return (
        f"No models available at {base}. Start LM Studio (or `lms server start`) "
        "and load a model (`lms load <model>`), or fix base_url via "
        "`lmloop config set base_url <url>`."
    )


def nudge_message() -> dict:
    """User-role nudge with structured metadata (not string-identity for rollback)."""
    return {
        "role": "user",
        "content": NUDGE_CONTINUE_TEXT,
        LMLOOP_META: META_NUDGE,
    }


def is_nudge_message(msg: dict) -> bool:
    return isinstance(msg, dict) and msg.get(LMLOOP_META) == META_NUDGE


def api_messages(messages: list) -> list:
    """Strip internal lmloop metadata keys before sending to the model API."""
    out = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        out.append({k: v for k, v in m.items() if not str(k).startswith("_")})
    return out

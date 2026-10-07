"""Local feature-usage events for product optimization (~/.lmloop/usage.jsonl).

Append-only JSONL; no network. Opt out with ``LMLOOP_USAGE=0``.

Instrument new touchpoints with one line::

    usage.record("area.name", optional_tag="value")

Or a decorator when a function boundary is the natural hook::

    @usage.tracked("area.name")
    def my_handler(...):
        ...
"""

from __future__ import annotations

import functools
import os
from pathlib import Path
from typing import Any, Callable, TypeVar

from .config import STATE_ROOT, utc_now
from .memory import append_jsonl

USAGE_PATH = STATE_ROOT / "usage.jsonl"
_DISABLED = frozenset({"0", "false", "no", "n", "off"})

_F = TypeVar("_F", bound=Callable[..., Any])


def enabled() -> bool:
    """Whether usage events are appended (default on)."""
    return os.environ.get("LMLOOP_USAGE", "1").strip().lower() not in _DISABLED


def record(feature: str, /, **detail: Any) -> None:
    """Append one usage event. ``detail`` must be JSON-serializable."""
    if not enabled() or not feature:
        return
    row: dict[str, Any] = {"ts": utc_now(), "feature": feature}
    if detail:
        row["detail"] = detail
    append_jsonl(USAGE_PATH, row)


def tracked(feature: str, /, **static_detail: Any) -> Callable[[_F], _F]:
    """Record ``feature`` (and optional static detail) each time ``fn`` runs."""

    def decorator(fn: _F) -> _F:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any):
            record(feature, **static_detail)
            return fn(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorator


def read_events(path: Path | None = None) -> list[dict]:
    """Load usage JSONL (tests and local analysis). Skips corrupt lines."""
    from .memory import read_jsonl

    return read_jsonl(path or USAGE_PATH)

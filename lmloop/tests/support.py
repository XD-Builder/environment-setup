"""Reusable unittest helpers (stdlib only — no pytest).

Table-driven cases use ``parametrize`` (``subTest`` under the hood). Optional
extras gate on ``requires_rich`` so rich-dependent tests share one skip path.
"""

from __future__ import annotations

import functools
import unittest
from collections.abc import Callable
from typing import Any, ParamSpec, TypeVar

P = ParamSpec("P")
R = TypeVar("R")


def rich_available() -> bool:
    from lmloop.markdown_view import _rich_available

    return _rich_available()


def requires_rich(method: Callable[P, R]) -> Callable[P, R]:
    """Skip the test when the optional rich extra is not installed."""

    @functools.wraps(method)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        self = args[0]
        if not isinstance(self, unittest.TestCase):
            raise TypeError("requires_rich expects a unittest.TestCase method")
        if not rich_available():
            self.skipTest("rich not installed")
        return method(*args, **kwargs)

    return wrapper  # type: ignore[return-value]


def parametrize(
    *cases: tuple[Any, ...],
    names: tuple[str, ...] | None = None,
) -> Callable[[Callable[..., None]], Callable[..., None]]:
    """Run ``method(self, …)`` once per case, labeling failures with ``subTest``.

    Example::

        @parametrize(
            (True, True),
            ("False", False),
            names=("raw", "want"),
        )
        def test_coerce_bool(self, raw, want):
            self.assertIs(coerce_bool(raw), want)
    """

    def decorator(method: Callable[..., None]) -> Callable[..., None]:
        @functools.wraps(method)
        def wrapper(self: unittest.TestCase, /, *args: Any, **kwargs: Any) -> None:
            if not cases:
                raise ValueError("parametrize requires at least one case")
            for case in cases:
                row = case if isinstance(case, tuple) else (case,)
                label = (
                    dict(zip(names, row, strict=True))
                    if names is not None
                    else {"case": row}
                )
                with self.subTest(**label):
                    method(self, *row, *args, **kwargs)

        return wrapper

    return decorator

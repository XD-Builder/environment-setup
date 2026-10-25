"""Terminal chrome (``console``) and markdown rendering (``markdown_view``)."""

import importlib
from typing import Any


def __getattr__(name: str) -> Any:
    mod = importlib.import_module(".console", __name__)
    return getattr(mod, name)


def __dir__() -> list[str]:
    mod = importlib.import_module(".console", __name__)
    return sorted(set(globals()) | set(dir(mod)))

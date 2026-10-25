"""Compatibility shim: same module object as ``lmloop.ui.markdown_view``."""

import sys

from .ui import markdown_view as _markdown_view

sys.modules[__name__] = _markdown_view

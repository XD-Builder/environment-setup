"""Model I/O: chat HTTP, SSE ingest, live printers, and LMS bring-up.

``stream`` does not import ``chat``, ``display``, or ``agent``.
``display`` may import ``stream``. ``chat`` may import ``stream`` and ``server``.
None of these import ``loop`` or ``graph``.
"""

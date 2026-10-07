"""Live-thread file manifest for ``/context`` (not durable ~/.lmloop memory).

Scans the in-memory message list for ``@`` references, attachments, image
parts, and successful ``read_file`` tool results. Leaf relative to the REPL:
no ``agent``, ``loop``, or ``graph`` imports.
"""

from dataclasses import dataclass, field
from pathlib import Path

from . import extract, memory, tools
from .files_index import prompt_file_mentions, resolve_user_path

ACTIVE_FILES_HEADING = "Active files"
DURABLE_MEMORY_HEADING = "Durable memory"
NO_SESSION_FILES = "(no files in this conversation)"


@dataclass(frozen=True)
class ContextFile:
    """One path named in the live message list."""
    path: str
    loaded: bool
    attached: bool = False
    image: bool = False
    spans: tuple = ()


@dataclass
class _FileBuild:
    path: str
    loaded: bool = False
    attached: bool = False
    image: bool = False
    spans: list = field(default_factory=list)


def _canon_path(path: str, workspace_root: "Path | None") -> str:
    try:
        return resolve_user_path(path, workspace_root).as_posix()
    except (OSError, RuntimeError, ValueError):
        return path


def _message_has_image(content) -> bool:
    if not isinstance(content, list):
        return False
    return any(
        isinstance(part, dict) and part.get("type") == "image_url"
        for part in content
    )


def _note_file(
    builds: dict, order: list, path: str, workspace_root, *,
    loaded: bool = False, attached: bool = False, image: bool = False,
    span: "tuple | None" = None,
) -> None:
    if not path:
        return
    key = _canon_path(path, workspace_root)
    row = builds.get(key)
    if row is None:
        row = _FileBuild(path=key)
        builds[key] = row
        order.append(key)
    if loaded:
        row.loaded = True
    if attached:
        row.attached = True
        row.loaded = True
    if image:
        row.image = True
        row.loaded = True
    if span and span not in row.spans:
        row.spans.append(span)
        row.loaded = True


def _note_read_result(builds, order, content, arguments, workspace_root) -> None:
    text = content if isinstance(content, str) else str(content)
    if text.startswith("ERROR") or text.startswith("DENIED"):
        return
    header = tools.parse_read_header(text)
    if header:
        label, start, end, total = header
        _note_file(builds, order, label, workspace_root, span=(start, end, total))
        return
    image_label = tools.parse_image_read_label(text)
    if image_label:
        _note_file(builds, order, image_label, workspace_root, image=True)
        return
    arg_path = tools.tool_path_argument(arguments)
    if arg_path:
        _note_file(builds, order, arg_path, workspace_root, loaded=True)


def active_context_files(messages: list, workspace_root: "Path | None" = None) -> list:
    """Files named in the live thread, in first-seen order.

    ``loaded`` is true when the file body is in the window: a successful
    ``read_file``, an ``@`` attachment excerpt, or an image part. Other ``@``
    paths are referenced only. ``/undo``, ``/new``, and a compact replace drop
    files by dropping the messages that held them.
    """
    results = {}
    for message in messages or []:
        if message.get("role") == "tool" and message.get("tool_call_id"):
            results[message["tool_call_id"]] = message.get("content")
    builds: dict = {}
    order: list = []
    for message in messages or []:
        role = message.get("role")
        if role == "user":
            content = message.get("content")
            has_image = _message_has_image(content)
            for kind, path in prompt_file_mentions(extract.flatten_content(content)):
                if kind == "attached":
                    _note_file(builds, order, path, workspace_root, attached=True)
                    continue
                suffix = Path(path).suffix.lower()
                if has_image and suffix in extract.IMAGE_SUFFIXES:
                    _note_file(builds, order, path, workspace_root, image=True)
                else:
                    _note_file(builds, order, path, workspace_root)
        elif role == "assistant":
            for call in message.get("tool_calls") or []:
                fn = call.get("function") or {}
                if (fn.get("name") or "") != tools.TOOL_READ_FILE:
                    continue
                content = results.get(call.get("id"))
                if content is None:
                    continue
                _note_read_result(
                    builds, order, content, fn.get("arguments"), workspace_root,
                )
    return [
        ContextFile(
            path=builds[key].path,
            loaded=builds[key].loaded,
            attached=builds[key].attached,
            image=builds[key].image,
            spans=tuple(builds[key].spans),
        )
        for key in order
    ]


def _display_path(path: str, workspace_root: "Path | None") -> str:
    if workspace_root is None:
        return path
    try:
        rel = Path(path).resolve().relative_to(Path(workspace_root).resolve())
    except (ValueError, OSError):
        return path
    text = rel.as_posix()
    return text or path


def _format_spans(spans: tuple) -> str:
    groups = []
    for start, end, total in spans:
        rendered = f"{start}-{end}"
        if groups and groups[-1][0] == total:
            groups[-1][1].append(rendered)
        else:
            groups.append((total, [rendered]))
    parts = []
    for total, ranges in groups:
        parts.append("lines " + ", ".join(ranges) + f" of {total}")
    return "; ".join(parts)


def _file_detail(row: ContextFile) -> str:
    if not row.loaded:
        try:
            if Path(row.path).is_dir():
                return "directory · not opened"
        except OSError:
            pass
        return "referenced · not loaded"
    bits = []
    if row.attached:
        bits.append("attached excerpt")
    if row.image:
        bits.append("image")
    if row.spans:
        span = _format_spans(row.spans)
        bits.append(span if bits else f"read · {span}")
    elif not bits:
        bits.append("read")
    return " · ".join(bits)


def _file_context_lines(files: list, workspace_root: "Path | None") -> list:
    """Each path on its own line so a long name is never clipped."""
    lines = [memory.ViewLine(ACTIVE_FILES_HEADING, "heading"), memory.ViewLine("")]
    if not files:
        lines.append(memory.ViewLine(f"  {NO_SESSION_FILES}", "muted"))
        return lines
    for row in files:
        shown = _display_path(row.path, workspace_root)
        lines.append(memory.ViewLine(
            f"  {shown}", "path" if row.loaded else "muted",
        ))
        lines.append(memory.ViewLine(
            f"    {_file_detail(row)}", "ok" if row.loaded else "muted",
        ))
    return lines


def context_view_lines(
    files: list, cfg: dict, workspace_root: "Path | None" = None,
) -> list:
    """Active files, then the readable injected-memory view."""
    lines = _file_context_lines(files, workspace_root)
    lines.append(memory.ViewLine(""))
    lines.append(memory.ViewLine(DURABLE_MEMORY_HEADING, "heading"))
    lines.append(memory.ViewLine(""))
    lines.extend(memory.injected_memory_lines(cfg))
    return lines


def format_session_context(
    files: list, durable: str, workspace_root: "Path | None" = None,
) -> str:
    """Plain active-file list plus a caller-supplied memory block."""
    lines = _file_context_lines(files, workspace_root)
    lines.append(memory.ViewLine(""))
    lines.append(memory.ViewLine(DURABLE_MEMORY_HEADING, "heading"))
    body = (durable or "").rstrip("\n")
    if body:
        lines.append(memory.ViewLine(""))
        for raw in body.splitlines():
            lines.append(memory.ViewLine(raw))
    return "\n".join(line.text for line in lines)

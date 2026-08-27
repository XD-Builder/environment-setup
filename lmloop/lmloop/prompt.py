"""Interactive prompt built on prompt_toolkit (REPL input only)."""

from pathlib import Path
from typing import Callable, Iterator, List, Tuple

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.filters import has_completions, is_searching
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.search import stop_search
from prompt_toolkit.shortcuts.prompt import CompleteStyle
from prompt_toolkit.styles import Style

from . import agent
from .config import STATE_ROOT
from .files_index import (
    at_completion_token,
    at_token_end,
    is_fs_completion_prefix,
    list_fs_paths,
    list_project_paths,
    normalize_typed_path,
    resolve_user_path,
    strip_at_quotes,
)
from .ui import Console, drain_tty_input, format_input_prompt, short_model_name, short_path, strip_ansi, terminal_size


class ExitREPL(Exception):
    """Raised when the user confirms exit with a second Ctrl-C."""


# Styles for the completion menu (slash vs @file/@dir + blurbs).
PROMPT_STYLE = Style.from_dict({
    "completion-menu": "bg:#1e1e1e",
    "completion-menu.completion": "fg:#d4d4d4 bg:#1e1e1e",
    "completion-menu.completion.current": "fg:#ffffff bg:#0e639c",
    "completion-menu.meta.completion": "fg:#9cdcfe bg:#1e1e1e",
    "completion-menu.meta.completion.current": "fg:#b5e2ff bg:#0e639c",
    # Per-completion styles (Completion.style / meta_style)
    "slash": "fg:#4ec9b0 bold",
    "at-file": "fg:#ce9178",
    "at-dir": "fg:#dcdcaa bold",
    "bottom-toolbar": "fg:#cccccc bg:#252526",
    # Decorative input prompt (cwd · model ›) — not sent to the model
    "prompt-cwd": "fg:#9cdcfe",
    "prompt-sep": "fg:#808080",
    "prompt-model": "fg:#4ec9b0",
    "prompt-mark": "bold",
})


def _short_blurb(name: str, desc: str) -> str:
    """Prefer a short blurb; strip redundant 'Skill: name —' prefixes."""
    desc = (desc or "").strip()
    if not desc:
        return ""
    for sep in (" — ", " - ", ": "):
        if sep in desc:
            left, right = desc.split(sep, 1)
            if name.lstrip("/").lower() in left.lower() or left.lower().startswith("skill"):
                return right.strip()[:72]
    return desc[:72]


def _rank_match(candidate: str, prefix: str) -> Tuple[int, int, str]:
    """Sort key: prefix matches first, then subsequence, then by length/name."""
    c, p = candidate.lower(), prefix.lower()
    if c.startswith(p):
        return (0, len(candidate), c)
    # subsequence fuzzy (typed chars appear in order)
    i = 0
    for ch in c:
        if i < len(p) and ch == p[i]:
            i += 1
    if i == len(p) and p:
        return (1, len(candidate), c)
    return (2, len(candidate), c)


def _filter_names(names: List[str], prefix: str) -> List[str]:
    """Filter+sort names by prefix, then light fuzzy match."""
    if not prefix:
        return sorted(names)
    ranked = []
    for n in names:
        rank = _rank_match(n, prefix)
        if rank[0] < 2:  # prefix or fuzzy hit
            ranked.append((rank, n))
    ranked.sort(key=lambda x: x[0])
    return [n for _, n in ranked]


def _at_insert_text(path: str) -> str:
    """Completion insert text; quote paths that contain whitespace."""
    if any(ch.isspace() for ch in path):
        return '@"' + path + '"'
    return "@" + path


def _lex_prompt_line(
    text: str, cwd: "Path | None" = None,
) -> "list[tuple[str, str]]":
    """Style /commands and @paths. Fragments concatenate to ``text`` exactly."""
    out: "list[tuple[str, str]]" = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in " \t":
            j = i + 1
            while j < n and text[j] in " \t":
                j += 1
            out.append(("", text[i:j]))
            i = j
            continue
        if ch == "@":
            end = at_token_end(text, i, cwd)
            if end:
                token = text[i:end]
                style = "class:at-dir" if token.endswith("/") else "class:at-file"
                out.append((style, token))
                i = end
                continue
        if ch == "/":
            j = i + 1
            while j < n and text[j] not in " \t":
                j += 1
            out.append(("class:slash", text[i:j]))
            i = j
            continue
        j = i + 1
        while j < n and text[j] not in " \t":
            j += 1
        out.append(("", text[i:j]))
        i = j
    return out


class LmloopPromptLexer(Lexer):
    """Color @path and /command tokens in the input buffer (not the menu)."""

    def __init__(self, cwd_provider: "Callable[[], Path | None] | None" = None):
        self._cwd_provider = cwd_provider or (lambda: None)

    def lex_document(self, document: Document):
        lines = document.lines
        cwd = self._cwd_provider()
        if cwd is not None:
            cwd = Path(cwd)

        def get_line(lineno: int):
            if lineno < 0 or lineno >= len(lines):
                return []
            return _lex_prompt_line(lines[lineno], cwd)

        return get_line


class LmloopCompleter(Completer):
    """Complete /slash commands (with blurbs) and @filepath tokens."""

    def __init__(
        self,
        commands_provider: Callable[[], list],
        cwd_provider: "Callable[[], Path | None] | None" = None,
    ):
        self._commands_provider = commands_provider
        self._cwd_provider = cwd_provider or (lambda: Path.cwd())

    def get_completions(self, document: Document, complete_event) -> Iterator[Completion]:
        text_before = document.text_before_cursor
        cwd = self._cwd_provider()
        if cwd is not None:
            cwd = Path(cwd)
        word = _current_token(text_before, cwd)

        if _in_skill_arg(text_before, word):
            prefix = "" if word.startswith("/") else word
            names = _filter_names(agent.list_skills(), prefix)
            for name in names:
                blurb = _short_blurb(name, agent.skill_blurb(name)) or "skill"
                yield Completion(
                    name,
                    start_position=-len(prefix),
                    display=name,
                    display_meta=blurb,
                    style="class:slash",
                )
            return

        if word.startswith("/"):
            cmds = list(self._commands_provider())
            names = [c.name for c in cmds]
            descs = {c.name: c.desc for c in cmds}
            for name in _filter_names(names, word):
                blurb = _short_blurb(name, descs.get(name, ""))
                yield Completion(
                    name,
                    start_position=-len(word),
                    display=name,
                    display_meta=blurb,
                    style="class:slash",
                )
            return

        if word.startswith("@"):
            prefix = strip_at_quotes(word[1:])
            if is_fs_completion_prefix(prefix):
                paths = list_fs_paths(prefix, cwd)
            else:
                paths = _filter_names(list_project_paths(cwd), prefix)
                seen = set(paths)
                fs_prefix = "./" + prefix if prefix else "./"
                for rel in list_fs_paths(fs_prefix, cwd):
                    if rel.startswith("./"):
                        rel = rel[2:]
                    if rel and rel not in seen:
                        seen.add(rel)
                        paths.append(rel)
            for path in paths:
                path = normalize_typed_path(path)
                is_dir = path.endswith("/")
                style = "class:at-dir" if is_dir else "class:at-file"
                meta = "dir" if is_dir else "file"
                if is_fs_completion_prefix(prefix):
                    try:
                        meta = str(resolve_user_path(path, cwd))
                    except (OSError, RuntimeError, ValueError):
                        pass
                insert = _at_insert_text(path)
                yield Completion(
                    insert,
                    start_position=-len(word),
                    display="@" + path,
                    display_meta=meta,
                    style=style,
                )


def _current_token(text_before: str, cwd: "Path | None" = None) -> str:
    """Token before cursor. @paths may include spaces while they match a file."""
    return at_completion_token(text_before, cwd)


def _at_slash_action(word: str, selected: "str | None") -> str:
    """How ``/`` should behave: apply a dir completion, refresh, or insert."""
    if word.startswith("@"):
        if selected and selected.startswith("@") and selected.endswith("/"):
            return "apply"
        if word.endswith("/"):
            return "refresh"
    return "insert"


def _in_skill_arg(text_before: str, word: str) -> bool:
    """True when the cursor is on the skill-name argument of ``/skill`` (not ``/skills``)."""
    stripped = text_before.lstrip()
    # Exact command /skill, not /skills or /skill-foo
    if stripped == "/skill" or stripped.startswith("/skill "):
        if word.startswith("/skill"):
            return False
        return True
    return False


def build_prompt_session(
    commands_provider: Callable[[], list],
    state_getter: Callable,
    color: bool = True,
) -> PromptSession:
    """Create a PromptSession with completers, toolbar, history, double Ctrl-C."""
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    history_path = STATE_ROOT / "history"

    exit_state = {"pending": False}

    def clear_exit_hint() -> None:
        exit_state["pending"] = False
        st = state_getter()
        if st is not None and hasattr(st, "_exit_hint"):
            st._exit_hint = False

    kb = KeyBindings()

    # Ctrl-C during reverse-i-search aborts search only (not REPL exit).
    @kb.add(Keys.ControlC, filter=is_searching, eager=True)
    def _ctrl_c_abort_search(event):
        clear_exit_hint()
        stop_search()
        event.app.invalidate()

    # Ctrl-C while / or @ completion is open: dismiss menu first (don't arm exit).
    @kb.add(Keys.ControlC, filter=has_completions, eager=True)
    def _ctrl_c_cancel_completion(event):
        clear_exit_hint()
        event.current_buffer.cancel_completion()
        event.app.invalidate()

    @kb.add(Keys.ControlC, filter=~is_searching & ~has_completions)
    def _ctrl_c(event):
        if exit_state["pending"]:
            event.app.exit(exception=ExitREPL())
            return
        exit_state["pending"] = True
        st = state_getter()
        if st is not None:
            st._exit_hint = True
        event.app.invalidate()

    # Ctrl-Space force-opens the completion menu (handy if it was dismissed).
    @kb.add("c-space")
    def _force_complete(event):
        b = event.app.current_buffer
        if b.complete_state:
            b.complete_next()
        else:
            b.start_completion(select_first=False)

    # In an @path token, '/' opens the highlighted (or current) directory
    # listing instead of inserting another slash (@~/Downloads/ + / → //).
    @kb.add("/", eager=True)
    def _slash_open_at_dir(event):
        b = event.current_buffer
        st = state_getter()
        cwd = getattr(st, "workspace_root", None) if st is not None else None
        word = _current_token(b.document.text_before_cursor, cwd)
        selected = None
        if b.complete_state and b.complete_state.current_completion:
            selected = b.complete_state.current_completion.text
        action = _at_slash_action(word, selected)
        if action == "apply":
            b.apply_completion(b.complete_state.current_completion)
            return
        if action == "refresh":
            b.complete_state = None
            b.start_completion(select_first=False)
            return
        b.insert_text("/")

    # Ctrl-O opens prior-turn thinking (oldest first, one pager). No cycle.
    # Mid-stream keys are drained before the prompt so they cannot steal the TTY.
    @kb.add("c-o")
    def _expand_collapse(event):
        clear_exit_hint()
        st = state_getter()
        if st is None:
            return
        body = st.prior_thinking_transcript()
        console = st.console or Console(color=color)
        event.app.renderer.erase()
        if not body:
            if st.thinking_count() <= 0:
                console.hint("[no prior thinking yet — live thinking shows as it streams]")
            else:
                console.hint(
                    "[only this turn's thinking is stored — it already streamed live]"
                )
        else:
            from .markdown_view import page_ansi
            page_ansi(body)
        event.app.invalidate()

    def bottom_toolbar():
        st = state_getter()
        if st is not None and getattr(st, "_exit_hint", False):
            return HTML("<b><style bg='ansired' fg='ansiwhite'> Ctrl-C again to exit </style></b>")
        if st is None:
            return ""
        console = Console(color=False)
        line = console.status_line(
            st.stats, st.user_turns, st.context_limit,
            st.messages, st.context_reserve, inline=True,
        )
        parts = []
        if line:
            parts.append(strip_ansi(line))
        n_prior = st.prior_thinking_count()
        if n_prior:
            parts.append(f"Ctrl-O · {n_prior} prior thinking")
        text = " · ".join(parts) if parts else ""
        cols, _ = terminal_size()
        if len(text) > cols:
            text = text[: max(0, cols - 1)] + "…"
        return text

    def message():
        """Decorative prompt only — cwd/model context; never LLM chat content."""
        st = state_getter()
        model = getattr(st, "model", "") if st is not None else ""
        if not color:
            return format_input_prompt(model)
        cwd = short_path()
        name = short_model_name(model)
        parts = [("class:prompt-cwd", cwd)]
        if name:
            parts.extend([
                ("class:prompt-sep", " · "),
                ("class:prompt-model", name),
            ])
        parts.extend([("", " "), ("class:prompt-mark", "› ")])
        return parts

    session = PromptSession(
        message=message,
        completer=LmloopCompleter(
            commands_provider,
            cwd_provider=lambda: getattr(state_getter(), "workspace_root", None),
        ),
        # Must be True for / and @ menus to open as you type (no Tab).
        # Incompatible with enable_history_search — keep that False.
        complete_while_typing=True,
        enable_history_search=False,
        # COLUMN shows display_meta (blurbs). MULTI_COLUMN hides them.
        complete_style=CompleteStyle.COLUMN,
        # Keep room for the menu above the input + bottom toolbar.
        reserve_space_for_menu=12,
        history=FileHistory(str(history_path)),
        key_bindings=kb,
        bottom_toolbar=bottom_toolbar,
        style=PROMPT_STYLE if color else None,
        lexer=LmloopPromptLexer(
            cwd_provider=lambda: getattr(state_getter(), "workspace_root", None),
        ) if color else None,
    )
    session.lmloop_clear_exit = clear_exit_hint  # type: ignore[attr-defined]

    def _on_text(_buffer):
        # Only touch the app when clearing a pending Ctrl-C exit — avoid
        # invalidate() on every keystroke (that can dismiss the completion menu).
        if exit_state["pending"] and _buffer.text:
            clear_exit_hint()

    session.default_buffer.on_text_changed += _on_text
    return session


def read_line(session: PromptSession) -> str:
    """Prompt for one line. Raises ExitREPL / EOFError to leave the REPL."""
    clear = getattr(session, "lmloop_clear_exit", None)
    if clear:
        clear()
    drain_tty_input()
    line = session.prompt()
    if clear:
        clear()
    return (line or "").strip()

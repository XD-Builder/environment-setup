"""Project file index for @path completion and reference expansion."""

import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

# Bare @path, @"quoted path", or @'quoted path'. Emails (user@host) are skipped
# by the leading (?<!\w). Unquoted tokens stop at whitespace or another @;
# collect_at_refs then extends the last component against the filesystem so
# unquoted names with spaces still resolve.
AT_REF_RE = re.compile(
    r"""(?<!\w)@(?:
        "([^"]+)"
      | '([^']+)'
      | ([^\s@]+)
    )""",
    re.VERBOSE,
)
# Collapse accidental // from completion (~/ + /Downloads → ~//Downloads).
# Keep a leading // (UNC) and the // after a URI scheme.
DUP_SLASH_RE = re.compile(r"/{2,}")
# After a complete filename/dir match, these end the @token (not '(' — "file(1).docx").
_AT_BOUNDARY = frozenset(" \t\n,.;:)]}")
_QUOTE_CHARS = frozenset({'"', "'"})
_MAX_PATH_PARTS = 32

IGNORE_DIRS = frozenset({
    ".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__",
    ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build",
    ".eggs", ".lmloop",
})

_CACHE: "dict[str, tuple[float, list[str]]]" = {}
_CACHE_TTL_S = 2.0
_MAX_PATHS = 800
_MAX_FS_COMPLETIONS = 80
_REF_BLOCK_MARKER = "Referenced files (use read_file"
_REF_BLOCK_HEADER = (
    "Referenced files (use read_file / list_dir with the resolved path "
    "in place; do not copy into the workspace):"
)


@dataclass(frozen=True)
class AtRef:
    """One user @path token that resolved to an existing file or directory."""
    token: str
    resolved: Path
    is_dir: bool


@dataclass(frozen=True)
class AtRefExpansion:
    """User text plus resolved @refs (and tokens that did not exist)."""
    text: str
    refs: tuple = ()
    missing: tuple = ()


def resolve_user_path(path: str, cwd: "Path | None" = None) -> Path:
    """Resolve ~, absolute, and cwd-relative path strings the same way tools do."""
    root = (cwd or Path.cwd()).resolve()
    p = Path(normalize_typed_path(path)).expanduser()
    return p.resolve() if p.is_absolute() else (root / p).resolve()


def normalize_typed_path(path: str) -> str:
    """Collapse duplicate slashes in a user-typed path, keeping ~/ and UNC/URI."""
    raw = path or ""
    if "://" in raw:
        scheme, rest = raw.split("://", 1)
        return scheme + "://" + DUP_SLASH_RE.sub("/", rest)
    if raw.startswith("//"):
        return "//" + DUP_SLASH_RE.sub("/", raw[2:])
    return DUP_SLASH_RE.sub("/", raw)


def join_typed_path(typed_dir: str, name: str, is_dir: bool = False) -> str:
    """Join a typed prefix (~/, /abs, ./) to a basename without producing //."""
    parent = normalize_typed_path(typed_dir)
    if parent != "/" and not parent.endswith("/"):
        parent += "/"
    child = (name or "").lstrip("/")
    suffix = "/" if is_dir else ""
    return parent + child + suffix


def _split_typed_dir(raw: str) -> "tuple[str, str]":
    """Split a typed path into (dir-with-slash-or-empty, last component)."""
    if "/" not in raw:
        return "", raw
    parent, name = raw.rsplit("/", 1)
    return parent + "/", name


def _opening_quote(inner: str) -> str:
    """Opening ``"`` or ``'``, else ``''``. Set membership so ``''`` is not a hit."""
    if inner and inner[0] in _QUOTE_CHARS:
        return inner[0]
    return ""


def strip_at_quotes(inner: str) -> str:
    """Strip matching or still-open quotes from the path inside an @token."""
    q = _opening_quote(inner)
    if not q:
        return inner
    if len(inner) >= 2 and inner[-1] == q:
        return inner[1:-1]
    return inner[1:]


def _ws_token(text_before: str) -> str:
    if not text_before:
        return ""
    i = len(text_before)
    while i > 0 and text_before[i - 1] not in " \t\n":
        i -= 1
    return text_before[i:]


def _last_bare_at(text: str) -> "int | None":
    """Index of the last @ that is not part of a word (skips user@host)."""
    i = len(text)
    while i > 0:
        i = text.rfind("@", 0, i)
        if i < 0:
            return None
        if i == 0 or not (text[i - 1].isalnum() or text[i - 1] == "_"):
            return i
    return None


def _longest_entry(scan: Path, haystack: str) -> "tuple[str, bool] | None":
    """Longest directory entry whose name is a prefix of haystack at a boundary."""
    try:
        entries = list(scan.iterdir())
    except OSError:
        return None
    best: "tuple[str, bool] | None" = None
    for entry in entries:
        name = entry.name
        if name in (".", "..") or not haystack.startswith(name):
            continue
        nxt = haystack[len(name): len(name) + 1]
        try:
            is_dir = entry.is_dir()
        except OSError:
            continue
        if nxt and nxt not in _AT_BOUNDARY and not (is_dir and nxt == "/"):
            continue
        if best is None or len(name) > len(best[0]):
            best = (name, is_dir)
    return best


def _extend_unquoted_path(raw: str, rest: str, cwd: Path) -> "tuple[int, str]":
    """Grow an unquoted token by complete filesystem names (spaces allowed).

    Returns (extra chars consumed from rest, extended typed path). Extra is 0
    when the original token already names an existing path — no rewrite.
    """
    raw = normalize_typed_path(raw)
    if not raw or raw.endswith("/"):
        return 0, raw
    typed_dir, name = _split_typed_dir(raw)
    try:
        scan = resolve_user_path(typed_dir or ".", cwd)
        if not scan.is_dir():
            return 0, raw
    except (OSError, RuntimeError, ValueError):
        return 0, raw
    haystack = name + rest
    remaining = haystack
    pieces: "list[str]" = []
    last_dir = False
    for _ in range(_MAX_PATH_PARTS):
        hit = _longest_entry(scan, remaining)
        if hit is None:
            break
        part, is_dir = hit
        pieces.append(part)
        remaining = remaining[len(part):]
        last_dir = is_dir
        if is_dir and remaining.startswith("/"):
            remaining = remaining[1:]
            try:
                scan = scan / part
                if not scan.is_dir():
                    last_dir = False
                    break
            except OSError:
                last_dir = False
                break
            continue
        break
    if not pieces:
        return 0, raw
    matched_len = len(haystack) - len(remaining)
    extra = matched_len - len(name)
    if extra <= 0:
        return 0, raw
    rel = "/".join(pieces)
    if typed_dir:
        new_raw = join_typed_path(typed_dir, rel, last_dir)
    else:
        new_raw = rel + ("/" if last_dir else "")
    return extra, new_raw


def _is_live_path_prefix(prefix: str, cwd: Path) -> bool:
    """True when prefix still matches a filesystem or project path being typed."""
    prefix = normalize_typed_path(prefix)
    if not prefix:
        return False
    if is_fs_completion_prefix(prefix) or "/" in prefix:
        if list_fs_paths(prefix, cwd):
            return True
    else:
        if list_fs_paths("./" + prefix, cwd):
            return True
        name = prefix.lower()
        for p in list_project_paths(cwd):
            if p.lower().startswith(name):
                return True
    try:
        return resolve_user_path(prefix, cwd).exists()
    except (OSError, RuntimeError, ValueError):
        return False


def _grow_live_prefix(raw: str, rest: str, cwd: Path) -> int:
    """Chars of rest that still form a live path prefix (for lexer / completion)."""
    grow = 0
    for j, ch in enumerate(rest):
        if ch == "@":
            break
        trial = raw + rest[: j + 1]
        if _is_live_path_prefix(trial, cwd):
            grow = j + 1
        elif ch in " \t":
            continue
        else:
            break
    return grow


def consume_at_ref(
    text: str, start: int, cwd: "Path | None" = None,
) -> "tuple[str, bool, int] | None":
    """If text[start:] is an @path, return (raw, quoted, end). Extends spaces."""
    m = AT_REF_RE.match(text, start)
    if not m:
        return None
    raw, quoted = _raw_at_token(m)
    raw = normalize_typed_path(raw)
    end = m.end()
    if not quoted and cwd is not None and raw:
        extra, raw = _extend_unquoted_path(raw, text[end:], cwd)
        end += extra
    return raw, quoted, end


def at_token_end(text: str, start: int, cwd: "Path | None" = None) -> "int | None":
    """End index of the @token at start, including a live typed prefix."""
    hit = consume_at_ref(text, start, cwd)
    if hit is None:
        return None
    raw, quoted, end = hit
    if quoted or cwd is None:
        return end
    return end + _grow_live_prefix(raw, text[end:], cwd)


def at_completion_token(text_before: str, cwd: "Path | None" = None) -> str:
    """@token before the cursor, including spaces while it is a live prefix."""
    if not text_before:
        return ""
    word = _ws_token(text_before)
    at = _last_bare_at(text_before)
    if at is None:
        return word
    candidate = text_before[at:]
    inner = candidate[1:]
    q = _opening_quote(inner)
    if q and q not in inner[1:]:
        return candidate
    if cwd is not None:
        typed = strip_at_quotes(inner)
        if typed and _is_live_path_prefix(typed, cwd):
            return candidate
    return word


def list_project_paths(cwd: "Path | None" = None, limit: int = _MAX_PATHS) -> "list[str]":
    """Relative project paths for completion (git-aware when possible)."""
    root = (cwd or Path.cwd()).resolve()
    key = str(root)
    now = time.monotonic()
    cached = _CACHE.get(key)
    if cached and (now - cached[0]) < _CACHE_TTL_S:
        return cached[1][:limit]

    paths = _git_paths(root)
    if paths is None:
        paths = _walk_paths(root)
    paths = sorted(set(paths))[:limit]
    _CACHE[key] = (now, paths)
    return paths


def clear_path_cache() -> None:
    _CACHE.clear()


def is_fs_completion_prefix(prefix: str) -> bool:
    """True when @ completion should scan the filesystem, not the project index."""
    return (
        prefix.startswith("~")
        or prefix.startswith("/")
        or prefix.startswith("./")
        or prefix.startswith("../")
        or prefix in (".", "..")
    )


def list_fs_paths(
    prefix: str,
    cwd: "Path | None" = None,
    limit: int = _MAX_FS_COMPLETIONS,
) -> "list[str]":
    """Completion candidates in the typed form (~/, /abs, ./, ../).

    A unique exact directory (with or without a trailing slash) lists that
    directory's entries, so the user does not have to type ``/`` to open it.
    """
    root = (cwd or Path.cwd()).resolve()
    prefix = normalize_typed_path(prefix)
    if prefix in ("~", ".", ".."):
        prefix = prefix + "/"
    if "/" not in prefix:
        return []
    typed_dir, name = prefix.rsplit("/", 1)
    typed_dir = typed_dir + "/"
    matches, exact_dir = _fs_dir_matches(typed_dir, name, root, limit)
    if exact_dir and len(matches) == 1:
        return list_fs_paths(exact_dir, root, limit)
    return matches


def _fs_dir_matches(
    typed_dir: str,
    name: str,
    root: Path,
    limit: int,
) -> "tuple[list[str], str | None]":
    """Sibling matches under ``typed_dir``, plus the exact-dir path if any."""
    try:
        scan = resolve_user_path(typed_dir, root)
    except (OSError, RuntimeError, ValueError):
        return [], None
    try:
        if not scan.is_dir():
            return [], None
        entries = sorted(scan.iterdir(), key=lambda e: e.name.lower())
    except OSError:
        return [], None
    out: "list[str]" = []
    exact_dir: "str | None" = None
    name_l = name.lower()
    show_hidden = name.startswith(".")
    for entry in entries:
        if not show_hidden and entry.name.startswith("."):
            continue
        if entry.name in IGNORE_DIRS:
            continue
        if name_l and not entry.name.lower().startswith(name_l):
            continue
        try:
            is_dir = entry.is_dir()
        except OSError:
            continue
        path = join_typed_path(typed_dir, entry.name, is_dir)
        if is_dir and name_l and entry.name.lower() == name_l:
            exact_dir = path
        out.append(path)
        if len(out) >= limit:
            break
    return out, exact_dir


def _git_paths(root: Path) -> "list[str] | None":
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-co", "--exclude-standard"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    out = []
    for line in proc.stdout.splitlines():
        line = line.strip().replace("\\", "/")
        if line and not line.endswith("/"):
            out.append(line)
    # Also include directory prefixes so @src/ can complete
    dirs = set()
    for p in out:
        parts = p.split("/")
        for i in range(1, len(parts)):
            dirs.add("/".join(parts[:i]) + "/")
    return out + sorted(dirs)


def _walk_paths(root: Path) -> "list[str]":
    out: "list[str]" = []
    dirs_out: "list[str]" = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS and not d.startswith(".")]
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir == ".":
            rel_dir = ""
        else:
            rel_dir = rel_dir.replace("\\", "/")
            if not rel_dir.endswith("/"):
                dirs_out.append(rel_dir + "/")
        for name in filenames:
            if name.startswith(".") and name not in (".env.example",):
                continue
            rel = f"{rel_dir}/{name}" if rel_dir else name
            out.append(rel.replace("\\", "/"))
            if len(out) >= _MAX_PATHS:
                return out + dirs_out
    return out + dirs_out


def _raw_at_token(match: re.Match) -> "tuple[str, bool]":
    """Return (token, quoted) from an AT_REF_RE match."""
    quoted = match.group(1) is not None or match.group(2) is not None
    raw = match.group(1) or match.group(2) or match.group(3) or ""
    if not quoted:
        raw = raw.rstrip(",.;:")
    return raw, quoted


def _rewrite_at_token(match: re.Match, normalized: str) -> str:
    """Rebuild an @path match with collapsed slashes; keep quotes and trailing punct."""
    if match.group(1) is not None:
        return '@"' + normalized + '"'
    if match.group(2) is not None:
        return "@'" + normalized + "'"
    orig = match.group(3) or ""
    punct = orig[len(orig.rstrip(",.;:")):]
    return "@" + normalized + punct


def collect_at_refs(text: str, cwd: "Path | None" = None) -> AtRefExpansion:
    """Resolve @path tokens; append a referenced-files block when any exist."""
    if not text or _REF_BLOCK_MARKER in text:
        return AtRefExpansion(text=text or "", refs=(), missing=())
    root = (cwd or Path.cwd()).resolve()
    refs: "list[AtRef]" = []
    missing: "list[str]" = []
    seen: set = set()
    parts: "list[str]" = []
    last = 0
    i = 0
    n = len(text)
    while i < n:
        m = AT_REF_RE.search(text, i)
        if not m:
            break
        raw, quoted = _raw_at_token(m)
        raw = normalize_typed_path(raw)
        end = m.end()
        extra = 0
        if not quoted and raw:
            extra, raw = _extend_unquoted_path(raw, text[end:], root)
            end += extra
        parts.append(text[last:m.start()])
        if quoted:
            parts.append(_rewrite_at_token(m, raw) if raw else m.group(0))
        else:
            orig = m.group(3) or ""
            punct = orig[len(orig.rstrip(",.;:")):] if extra == 0 else ""
            parts.append("@" + raw + punct if raw else m.group(0))
        last = end
        i = max(end, m.start() + 1)
        if not raw or raw in seen:
            continue
        seen.add(raw)
        try:
            path = resolve_user_path(raw, root)
            is_dir = path.is_dir()
            is_file = path.is_file()
        except (OSError, RuntimeError, ValueError):
            missing.append(raw)
            continue
        if is_dir or is_file:
            refs.append(AtRef(token=raw, resolved=path, is_dir=is_dir))
        else:
            missing.append(raw)
    rewritten = "".join(parts) + text[last:]
    if not refs:
        return AtRefExpansion(text=rewritten, refs=(), missing=tuple(missing))
    lines = [f"- @{r.token} → {r.resolved.as_posix()}" for r in refs]
    block = _REF_BLOCK_HEADER + "\n" + "\n".join(lines)
    new_text = rewritten.rstrip() + "\n\n" + block + "\n"
    return AtRefExpansion(text=new_text, refs=tuple(refs), missing=tuple(missing))


def expand_at_refs(text: str, cwd: "Path | None" = None) -> str:
    """Append a referenced-files block for existing @path tokens."""
    return collect_at_refs(text, cwd).text

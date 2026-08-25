"""Project file index for @path completion and reference expansion."""

import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

# Bare @path, @"quoted path", or @'quoted path'. Emails (user@host) are skipped
# by the leading (?<!\w). Unquoted tokens stop at whitespace or another @.
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
    """Completion candidates in the typed form (~/, /abs, ./, ../)."""
    root = (cwd or Path.cwd()).resolve()
    if prefix in ("~", ".", ".."):
        prefix = prefix + "/"
    if "/" not in prefix:
        return []
    typed_dir, name = prefix.rsplit("/", 1)
    typed_dir = typed_dir + "/"
    try:
        scan = resolve_user_path(typed_dir, root)
    except (OSError, RuntimeError, ValueError):
        return []
    try:
        if not scan.is_dir():
            return []
        entries = sorted(scan.iterdir(), key=lambda e: e.name.lower())
    except OSError:
        return []
    out: "list[str]" = []
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
        out.append(join_typed_path(typed_dir, entry.name, is_dir))
        if len(out) >= limit:
            break
    return out


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
    for m in AT_REF_RE.finditer(text):
        raw, _quoted = _raw_at_token(m)
        raw = normalize_typed_path(raw)
        parts.append(text[last:m.start()])
        parts.append(_rewrite_at_token(m, raw) if raw else m.group(0))
        last = m.end()
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

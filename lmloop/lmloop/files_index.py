"""Project file index for @path completion and reference expansion."""

import os
import re
import subprocess
import time
from pathlib import Path

AT_REF_RE = re.compile(r"(?<!\w)@([^\s@]+)")

IGNORE_DIRS = frozenset({
    ".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__",
    ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build",
    ".eggs", ".lmloop",
})

_CACHE: "dict[str, tuple[float, list[str]]]" = {}
_CACHE_TTL_S = 2.0
_MAX_PATHS = 800


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


_REF_BLOCK_MARKER = "Referenced files (use read_file):"


def expand_at_refs(text: str, cwd: "Path | None" = None) -> str:
    """Append a referenced-files block for existing @path tokens under cwd."""
    if not text or _REF_BLOCK_MARKER in text:
        return text
    root = (cwd or Path.cwd()).resolve()
    found: "list[str]" = []
    seen = set()
    for m in AT_REF_RE.finditer(text):
        raw = m.group(1).rstrip("/,.;:")
        if not raw or raw in seen:
            continue
        seen.add(raw)
        path = (root / raw).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            continue
        if path.is_file():
            found.append(raw)
    if not found:
        return text
    block = _REF_BLOCK_MARKER + "\n" + "\n".join(f"- {p}" for p in found)
    return text.rstrip() + "\n\n" + block + "\n"

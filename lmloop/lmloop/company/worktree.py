"""Git worktrees under ``.lmloop/worktrees`` so the Docker mount stays inside the repo."""

from __future__ import annotations

import subprocess
from pathlib import Path


class WorktreeError(RuntimeError):
    """git could not create or merge a company worktree."""


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=check, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        raise WorktreeError(detail or f"git {' '.join(args)} failed") from exc
    except OSError as exc:
        raise WorktreeError(str(exc)) from exc


def head_sha(repo: Path) -> str:
    proc = git(repo, "rev-parse", "HEAD")
    sha = (proc.stdout or "").strip()
    if not sha:
        raise WorktreeError(f"{repo} has no HEAD")
    return sha


def _dest(repo: Path, name: str) -> Path:
    if name.startswith(".") or "/" in name or name in ("", ".", ".."):
        raise WorktreeError(f"refusing worktree name {name!r}")
    return repo / ".lmloop" / "worktrees" / name


def ensure_worktree(repo: Path, name: str, base_sha: str) -> Path:
    dest = _dest(repo, name)
    if (dest / ".git").exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "-B", f"lmloop/{name}", str(dest), base_sha)
    return dest


def ensure_clone(repo: Path, name: str, base_sha: str) -> Path:
    dest = _dest(repo, name)
    if (dest / ".git").exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "clone", "--local", str(repo.resolve()), str(dest))
    git(dest, "checkout", "--detach", base_sha)
    return dest


def ensure_isolation(repo: Path, name: str, base_sha: str, isolation: str) -> Path:
    if isolation == "clone":
        return ensure_clone(repo, name, base_sha)
    return ensure_worktree(repo, name, base_sha)


def merge_into(integration: Path, source: Path) -> "tuple[bool, str]":
    """Merge ``source`` HEAD into ``integration``. Conflict aborts and returns False."""
    sha = head_sha(source)
    proc = git(integration, "merge", "--no-ff", "--no-edit", sha, check=False)
    if proc.returncode == 0:
        return True, (proc.stdout or "merged").strip()
    git(integration, "merge", "--abort", check=False)
    detail = (proc.stderr or proc.stdout or "merge failed").strip()
    return False, detail

"""Git worktrees under ``.lmloop/worktrees`` so the Docker mount stays inside the repo.

Feature-plan isolation uses the same directory and ``ensure_worktree``. Removal
never passes ``--force``.
"""

from __future__ import annotations

import shutil
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


def _abs_git_path(repo: Path, raw: str) -> Path:
    path = Path((raw or "").strip())
    if not path.is_absolute():
        path = repo / path
    return path.resolve()


def is_linked_worktree(repo: Path) -> bool:
    """True when ``repo`` is a linked worktree and not a submodule."""
    superproject = git(
        repo, "rev-parse", "--show-superproject-working-tree", check=False,
    )
    if (superproject.stdout or "").strip():
        return False
    git_dir = git(repo, "rev-parse", "--git-dir", check=False)
    common = git(repo, "rev-parse", "--git-common-dir", check=False)
    if git_dir.returncode != 0 or common.returncode != 0:
        return False
    return _abs_git_path(repo, git_dir.stdout) != _abs_git_path(repo, common.stdout)


def worktrees_ignored(repo: Path) -> bool:
    """True when ``.lmloop/worktrees`` or ``.lmloop`` is gitignored."""
    for rel in (".lmloop/worktrees", ".lmloop"):
        proc = git(repo, "check-ignore", "-q", rel, check=False)
        if proc.returncode == 0:
            return True
    return False


def feature_worktree(repo: Path, name: str) -> Path:
    """Create ``.lmloop/worktrees/<name>`` on ``lmloop/<name>`` from HEAD.

    Refuses when that directory is not gitignored, so the tree cannot be
    committed by accident.
    """
    if not worktrees_ignored(repo):
        raise WorktreeError(".lmloop/worktrees is not gitignored")
    return ensure_worktree(repo, name, head_sha(repo))


def owned_worktree(repo: Path, path: Path) -> bool:
    """True when ``path`` is under this repo's ``.lmloop/worktrees``."""
    root = (repo / ".lmloop" / "worktrees").resolve()
    try:
        path.resolve().relative_to(root)
    except ValueError:
        return False
    return True


def remove_owned_worktree(repo: Path, path: Path) -> "tuple[bool, str]":
    """Remove a worktree lmloop created. Never passes ``--force``."""
    if not owned_worktree(repo, path):
        return False, "refusing to remove a worktree lmloop did not create"
    proc = git(repo, "worktree", "remove", str(path), check=False)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "worktree removal refused").strip()
        return False, detail
    git(repo, "worktree", "prune", check=False)
    return True, "removed"


def current_branch(repo: Path) -> str:
    proc = git(repo, "rev-parse", "--abbrev-ref", "HEAD", check=False)
    name = (proc.stdout or "").strip()
    if proc.returncode != 0 or not name or name == "HEAD":
        raise WorktreeError("could not name the current branch")
    return name


def main_checkout(repo: Path) -> "Path | None":
    """The main worktree, which git lists first. None when git cannot say."""
    proc = git(repo, "worktree", "list", "--porcelain", check=False)
    if proc.returncode != 0:
        return None
    for line in (proc.stdout or "").splitlines():
        if line.startswith("worktree "):
            return Path(line[len("worktree "):])
    return None


def detect_base(repo: Path) -> str:
    """Fork base: ``main`` when it exists, otherwise ``master``."""
    for name in ("main", "master"):
        proc = git(repo, "rev-parse", "--verify", "--quiet", name, check=False)
        if proc.returncode == 0:
            return name
    return "master"


def land_branch(
    repo: Path, choice: str, *, base: str, branch: str,
    worktree_path: "Path | None" = None,
) -> "tuple[bool, str]":
    """Merge, push a PR, or keep. Does not discard."""
    if choice == "keep":
        where = f" Worktree preserved at {worktree_path}." if worktree_path else ""
        return True, f"Keeping branch {branch}.{where}"
    if choice == "merge":
        git(repo, "checkout", base)
        proc = git(repo, "merge", "--no-ff", "--no-edit", branch, check=False)
        if proc.returncode != 0:
            git(repo, "merge", "--abort", check=False)
            detail = (proc.stderr or proc.stdout or "merge failed").strip()
            return False, detail
        if worktree_path is None:
            return True, f"merged {branch} into {base}"
        ok, detail = remove_owned_worktree(repo, worktree_path)
        if not ok:
            return False, f"merged, worktree removal refused: {detail}"
        git(repo, "branch", "-d", branch, check=False)
        return True, f"merged {branch} into {base} and removed the worktree"
    if choice == "pr":
        proc = git(repo, "push", "-u", "origin", branch, check=False)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "push failed").strip()
            return False, f"{detail}\nRun: git push -u origin {branch}"
        if shutil.which("gh") is None:
            return True, (
                f"Pushed {branch}. gh is not installed. "
                f"Open a pull request against {base}."
            )
        created = subprocess.run(
            ["gh", "pr", "create", "--base", base, "--head", branch, "--fill"],
            cwd=str(repo), capture_output=True, text=True, check=False,
        )
        if created.returncode != 0:
            detail = (created.stderr or created.stdout or "gh pr create failed").strip()
            return False, detail
        return True, (created.stdout or "pull request created").strip()
    return False, f"unknown finish choice {choice}"


def plan_workspace_git(repo: Path) -> dict:
    """Git facts and callables the plan host needs. Safe outside a git repo."""
    branch = ""
    try:
        branch = current_branch(repo)
    except WorktreeError:
        branch = ""
    return {
        "linked": is_linked_worktree(repo),
        "branch": branch,
        "base": detect_base(repo),
        "open_worktree": feature_worktree,
        "land": land_branch,
        "discard": discard_work,
        "main_checkout": lambda: main_checkout(repo),
    }


def discard_work(
    repo: Path, branch: str, worktree_path: "Path | None" = None,
) -> "tuple[bool, str]":
    """Delete a lmloop worktree and the branch. Caller checks the discard word."""
    if worktree_path is not None:
        ok, detail = remove_owned_worktree(repo, worktree_path)
        if not ok:
            return False, detail
    proc = git(repo, "branch", "-D", branch, check=False)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "branch delete failed").strip()
        return False, detail
    return True, f"discarded {branch}"


def merge_into(integration: Path, source: Path) -> "tuple[bool, str]":
    """Merge ``source`` HEAD into ``integration``. Conflict aborts and returns False."""
    sha = head_sha(source)
    proc = git(integration, "merge", "--no-ff", "--no-edit", sha, check=False)
    if proc.returncode == 0:
        return True, (proc.stdout or "merged").strip()
    git(integration, "merge", "--abort", check=False)
    detail = (proc.stderr or proc.stdout or "merge failed").strip()
    return False, detail

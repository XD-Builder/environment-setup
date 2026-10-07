"""Git workspace snapshots before autonomous maker steps (host path, leaf module).

Uses a temporary index so tracked edits and untracked files are captured without
touching the user's real index or working tree. Refs live under refs/lmloop/*.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

REF_PREFIX = "refs/lmloop/"
SNAPSHOT_RETENTION_DAYS = 14
SNAPSHOT_MAX_FILE_MB = 20


@dataclass(frozen=True)
class SnapshotResult:
    """Outcome of one snapshot attempt."""

    ref: str = ""
    skipped: tuple[str, ...] = ()
    note: str = ""


def _mode(cfg: dict) -> str:
    return str(cfg.get("autonomous_snapshot") or "git").strip().lower()


def git_root(workspace: Path) -> Path | None:
    """Return the git toplevel containing ``workspace``, or None."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    top = proc.stdout.strip()
    return Path(top).resolve() if top else None


def _git(
    root: Path,
    *args: str,
    env: dict | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        timeout=120,
        env=merged,
        check=False,
    )
    if check and proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(err or f"git {' '.join(args)} failed")
    return proc


def _head_tree(root: Path) -> str:
    proc = _git(root, "rev-parse", "HEAD^{tree}", check=True)
    return proc.stdout.strip()


def _oversize_untracked(root: Path, max_bytes: int) -> list[str]:
    proc = _git(
        root,
        "ls-files",
        "-o",
        "--exclude-standard",
        "-z",
        check=True,
    )
    raw = proc.stdout
    if not raw:
        return []
    skipped: list[str] = []
    for part in raw.split("\0"):
        if not part:
            continue
        path = root / part
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > max_bytes:
            skipped.append(part)
    return skipped


def _prune_old_refs(root: Path) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=SNAPSHOT_RETENTION_DAYS)
    proc = _git(
        root,
        "for-each-ref",
        "--format=%(refname) %(creatordate:iso-strict)",
        REF_PREFIX,
        check=False,
    )
    if proc.returncode != 0:
        return
    for line in proc.stdout.splitlines():
        parts = line.strip().split(" ", 1)
        if len(parts) != 2:
            continue
        ref, when = parts[0], parts[1]
        try:
            ts = datetime.fromisoformat(when.replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts < cutoff:
            _git(root, "update-ref", "-d", ref, check=False)


def take_snapshot(
    cfg: dict,
    workspace: Path,
    *,
    run_label: str,
    step: int,
) -> SnapshotResult:
    """Capture workspace state under refs/lmloop/<run_label>/<step> when enabled."""
    if _mode(cfg) == "off":
        return SnapshotResult(note="snapshots off")
    root = git_root(workspace)
    if root is None:
        return SnapshotResult(note="not a git repository")

    safe_label = "".join(c if c.isalnum() or c in "-_." else "-" for c in run_label)
    ref_name = f"{REF_PREFIX}{safe_label}/{step}"
    max_bytes = SNAPSHOT_MAX_FILE_MB * 1024 * 1024
    try:
        skipped = tuple(_oversize_untracked(root, max_bytes))
    except RuntimeError as exc:
        return SnapshotResult(note=str(exc))

    try:
        return _write_snapshot(root, ref_name, safe_label, step, skipped)
    except RuntimeError as exc:
        return SnapshotResult(note=str(exc))


def _write_snapshot(
    root: Path,
    ref_name: str,
    safe_label: str,
    step: int,
    skipped: tuple[str, ...],
) -> SnapshotResult:
    with tempfile.TemporaryDirectory(prefix="lmloop-snap-") as tmp:
        index_path = Path(tmp) / "index"
        env = {"GIT_INDEX_FILE": str(index_path)}
        _git(root, "read-tree", "HEAD", env=env, check=True)
        add_args = ["add", "-A", "--", "."]
        for rel in skipped:
            add_args.append(f":(exclude){rel}")
        _git(root, *add_args, env=env, check=True)
        tree_proc = _git(root, "write-tree", env=env, check=True)
        new_tree = tree_proc.stdout.strip()

    if new_tree == _head_tree(root):
        _prune_old_refs(root)
        return SnapshotResult(ref="HEAD", skipped=skipped)

    msg = f"lmloop snapshot {safe_label} {step}"
    commit_proc = _git(
        root,
        "commit-tree",
        new_tree,
        "-p",
        "HEAD",
        "-m",
        msg,
        check=True,
    )
    sha = commit_proc.stdout.strip()
    _git(root, "update-ref", ref_name, sha, check=True)
    _prune_old_refs(root)
    return SnapshotResult(ref=ref_name, skipped=skipped)


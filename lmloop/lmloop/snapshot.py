"""Git workspace snapshots before autonomous maker steps (host default path).

Uses a temporary index so the user's index and working tree are untouched.
Leaf module: no imports from loop, graph, or agent.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

SNAPSHOT_MAX_FILE_MB = 20
SNAPSHOT_RETENTION_DAYS = 14
REF_PREFIX = "refs/lmloop/"


@dataclass(frozen=True)
class SnapshotResult:
    """Outcome of one snapshot attempt (always safe to ignore for the run)."""

    snapshot_ref: str = ""
    tree: str = ""
    skipped: tuple[str, ...] = ()
    note: str = ""


def _run_git(
    root: Path,
    *args: str,
    env: "dict | None" = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(
        ["git", "-C", str(root), *args],
        env=merged,
        capture_output=True,
        text=True,
        check=check,
    )


def is_git_repo(root: Path) -> bool:
    try:
        proc = _run_git(root, "rev-parse", "--git-dir", check=False)
    except OSError:
        return False
    return proc.returncode == 0 and bool((proc.stdout or "").strip())


def snapshot_enabled(cfg: "dict | None") -> bool:
    mode = (cfg or {}).get("autonomous_snapshot") or "git"
    return str(mode).strip().lower() == "git"


def log_fields(result: SnapshotResult | None) -> dict:
    if result is None:
        return {}
    row: dict = {}
    if result.snapshot_ref:
        row["snapshot_ref"] = result.snapshot_ref
    if result.tree:
        row["tree"] = result.tree
    if result.skipped:
        row["snapshot_skipped"] = list(result.skipped)
    if result.note:
        row["snapshot_note"] = result.note
    return row


def _head_tree(root: Path) -> str:
    proc = _run_git(root, "rev-parse", "HEAD^{tree}")
    return (proc.stdout or "").strip()


def _oversized_untracked(root: Path, max_bytes: int) -> list[str]:
    proc = _run_git(
        root,
        "ls-files",
        "-o",
        "--exclude-standard",
        check=False,
    )
    if proc.returncode != 0:
        return []
    skipped: list[str] = []
    for rel in (proc.stdout or "").splitlines():
        rel = rel.strip()
        if not rel:
            continue
        path = root / rel
        try:
            if path.is_file() and path.stat().st_size > max_bytes:
                skipped.append(rel)
        except OSError:
            continue
    return skipped


def take_snapshot(
    root: Path,
    cfg: "dict | None",
    run_id: str,
    step: int,
) -> SnapshotResult:
    """Capture workspace state under ``refs/lmloop/<run_id>/<step>`` when possible."""
    if not snapshot_enabled(cfg):
        return SnapshotResult(note="snapshots off (autonomous_snapshot)")
    root = root.resolve()
    if not is_git_repo(root):
        return SnapshotResult(note="snapshots off (not a git repository)")
    max_bytes = SNAPSHOT_MAX_FILE_MB * 1024 * 1024
    skipped = tuple(_oversized_untracked(root, max_bytes))
    head_tree = _head_tree(root)
    idx_dir = tempfile.mkdtemp(prefix="lmloop-snap-")
    index_path = os.path.join(idx_dir, "index")
    env = {"GIT_INDEX_FILE": index_path}
    try:
        _run_git(root, "read-tree", "HEAD", env=env)
        add_args = ["add", "-A", "--", "."]
        for rel in skipped:
            add_args.append(f":(exclude){rel}")
        _run_git(root, *add_args, env=env)
        proc = _run_git(root, "write-tree", env=env)
        tree = (proc.stdout or "").strip()
        if not tree:
            return SnapshotResult(
                tree=head_tree,
                skipped=skipped,
                note="snapshot failed (empty tree)",
            )
        if tree == head_tree:
            return SnapshotResult(snapshot_ref="HEAD", tree=head_tree, skipped=skipped)
        msg = f"lmloop snapshot {run_id} {step}"
        commit = _run_git(root, "commit-tree", tree, "-p", "HEAD", "-m", msg)
        sha = (commit.stdout or "").strip()
        ref = f"{REF_PREFIX}{run_id}/{step}"
        _run_git(root, "update-ref", ref, sha)
        return SnapshotResult(snapshot_ref=ref, tree=tree, skipped=skipped)
    except (subprocess.CalledProcessError, OSError) as exc:
        err = getattr(exc, "stderr", None) or str(exc)
        return SnapshotResult(
            tree=head_tree,
            skipped=skipped,
            note=f"snapshot failed ({err.strip()[:200]})",
        )
    finally:
        try:
            os.unlink(index_path)
        except OSError:
            pass
        try:
            os.rmdir(idx_dir)
        except OSError:
            pass


def prune_old_refs(root: Path, cfg: "dict | None" = None) -> None:
    """Drop ``refs/lmloop/*`` older than ``SNAPSHOT_RETENTION_DAYS``."""
    if not snapshot_enabled(cfg):
        return
    root = root.resolve()
    if not is_git_repo(root):
        return
    cutoff = datetime.now(timezone.utc) - timedelta(days=SNAPSHOT_RETENTION_DAYS)
    proc = _run_git(
        root,
        "for-each-ref",
        "--format=%(refname)\t%(creatordate:iso8601-strict)",
        REF_PREFIX,
        check=False,
    )
    if proc.returncode != 0:
        return
    for line in (proc.stdout or "").splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        refname, created = parts[0].strip(), parts[1].strip()
        if not refname.startswith(REF_PREFIX):
            continue
        try:
            when = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when < cutoff:
            _run_git(root, "update-ref", "-d", refname, check=False)

"""Stable harness bundle fingerprint (H1). Leaf: config, steer, skills, tools only."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .. import __version__, steer
from ..core.config import Config, DEFAULTS, load_config
from ..skills import SKILLS_DIR
from ..tools import tool_names

_SYSTEM_SKILL = SKILLS_DIR / "system.md"


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    try:
        return _sha256_text(path.read_text(encoding="utf-8"))
    except OSError:
        return ""


def _steer_hashes(workspace_root: Path | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for directory in steer.steer_dirs(workspace_root):
        if not directory.is_dir():
            continue
        label = directory.name
        if directory == steer.STEER_DIR:
            label = "packaged"
        elif directory == steer.USER_STEER_DIR:
            label = "user"
        else:
            label = "project"
        for path in sorted(directory.glob("*.md")):
            if steer._invalid_steer_name(path.stem):
                continue
            digest = _sha256_file(path)
            if digest:
                out[f"{label}/{path.stem}"] = digest
    return out


def _config_slice(cfg: Config) -> dict[str, Any]:
    keys = (
        "autonomous_gates",
        "until_control",
        "until_max_steps",
        "sandbox_network",
        "eval_max_rounds",
    )
    merged = {**DEFAULTS, **cfg}
    return {key: merged.get(key) for key in keys}


def _tool_names(cfg: Config) -> list[str]:
    from .. import tools as tools_mod

    if tools_mod._TOOL_DEFS:
        return sorted(tools_mod._TOOL_DEFS.keys())
    return sorted(str(name) for name in tool_names(cfg=cfg))


def check_fingerprint(checks: list[dict] | None) -> str:
    """Hash sorted check commands from until meta ``checks`` rows."""
    cmds: list[str] = []
    for row in checks or []:
        if not isinstance(row, dict):
            continue
        cmd = str(row.get("cmd") or "").strip()
        if cmd:
            cmds.append(cmd)
    if not cmds:
        return ""
    payload = json.dumps(sorted(set(cmds)), ensure_ascii=False, separators=(",", ":"))
    return _sha256_text(payload)


def bundle_descriptor(
    cfg: Config | None = None,
    *,
    workspace_root: Path | None = None,
    graph_name: str = "",
    checks: list[dict] | None = None,
) -> dict[str, Any]:
    """JSON-serializable harness bundle. Workspace source edits are excluded."""
    resolved = load_config() if cfg is None else Config({**DEFAULTS, **cfg})
    descriptor: dict[str, Any] = {
        "version": __version__,
        "system_skill": _sha256_file(_SYSTEM_SKILL),
        "steer": _steer_hashes(workspace_root),
        "tool_names": _tool_names(resolved),
        "config": _config_slice(resolved),
    }
    fp = check_fingerprint(checks)
    if fp:
        descriptor["check_fingerprint"] = fp
    name = (graph_name or "").strip()
    if name:
        descriptor["graph_name"] = name
    return descriptor


def bundle_id(
    cfg: Config | None = None,
    *,
    workspace_root: Path | None = None,
    graph_name: str = "",
    checks: list[dict] | None = None,
) -> str:
    """SHA-256 hex digest of the canonical bundle descriptor."""
    raw = json.dumps(
        bundle_descriptor(
            cfg,
            workspace_root=workspace_root,
            graph_name=graph_name,
            checks=checks,
        ),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return _sha256_text(raw)


def attach_harness_meta(
    meta: dict,
    cfg: Config | None = None,
    *,
    workspace_root: Path | None = None,
    graph_name: str = "",
    checks: list[dict] | None = None,
) -> dict:
    """Add ``harness_bundle_id`` to a run meta row (mutates and returns ``meta``)."""
    meta["harness_bundle_id"] = bundle_id(
        cfg,
        workspace_root=workspace_root,
        graph_name=graph_name,
        checks=checks,
    )
    return meta

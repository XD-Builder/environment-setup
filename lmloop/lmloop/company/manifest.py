"""Company manifest (JSON). The topology lives in the repo and is reviewed like code."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

MANIFEST_REL = Path(".lmloop") / "company" / "manifest.json"


class ManifestError(ValueError):
    """The manifest is missing or does not match the v1 contract."""


@dataclass(frozen=True)
class Gate:
    cmd: str
    role: str


@dataclass(frozen=True)
class Milestone:
    id: str
    name: str
    gates: tuple
    on_pass: str


@dataclass(frozen=True)
class RoleBind:
    name: str
    worktree: str
    graph_nodes: tuple


@dataclass(frozen=True)
class Manifest:
    version: int
    integration_branch: str
    model_default: str
    model_eval: str
    models_roles: dict
    max_parallel_workers: int
    graph: str
    isolation: str
    milestones: tuple
    roles: tuple
    path: Path
    sha: str

    def role_for_node(self, node: str) -> "RoleBind | None":
        for role in self.roles:
            if node in role.graph_nodes:
                return role
        return None

    def model_for(self, node: str, role: "RoleBind | None") -> str:
        if node in self.models_roles:
            return self.models_roles[node]
        if role is not None and role.name in self.models_roles:
            return self.models_roles[role.name]
        return self.model_default

    def model_ids(self) -> list[str]:
        ids = [self.model_default, self.model_eval]
        ids.extend(self.models_roles.values())
        out: list[str] = []
        seen: set[str] = set()
        for item in ids:
            if item and item not in seen:
                seen.add(item)
                out.append(item)
        return out


def canonical_sha(data: dict) -> str:
    blob = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


def _gates(raw) -> tuple:
    gates = []
    for item in raw or []:
        if not isinstance(item, dict) or not str(item.get("cmd") or "").strip():
            raise ManifestError("each milestone gate needs a cmd")
        gates.append(Gate(cmd=str(item["cmd"]).strip(), role=str(item.get("role") or "check")))
    return tuple(gates)


def _roles(raw) -> tuple:
    if not isinstance(raw, dict) or not raw:
        raise ManifestError("manifest roles must be a non-empty object")
    roles = []
    for name, body in raw.items():
        if not isinstance(body, dict):
            raise ManifestError(f"role {name} must be an object")
        nodes = body.get("graph_nodes") or []
        if not isinstance(nodes, list) or not nodes:
            raise ManifestError(f"role {name} needs graph_nodes")
        roles.append(RoleBind(
            name=str(name),
            worktree=str(body.get("worktree") or ""),
            graph_nodes=tuple(str(n) for n in nodes),
        ))
    return tuple(roles)


def parse_manifest(data: dict, path: Path) -> Manifest:
    if not isinstance(data, dict):
        raise ManifestError("manifest must be a JSON object")
    if data.get("version") != 1:
        raise ManifestError("manifest version must be 1")
    models = data.get("models") or {}
    if not isinstance(models, dict):
        raise ManifestError("manifest models must be an object")
    role_models = models.get("roles") or {}
    if not isinstance(role_models, dict):
        raise ManifestError("manifest models.roles must be an object")
    resources = data.get("resources") or {}
    if not isinstance(resources, dict):
        raise ManifestError("manifest resources must be an object")
    try:
        parallel = int(resources.get("max_parallel_workers") or 2)
    except (TypeError, ValueError) as exc:
        raise ManifestError("max_parallel_workers must be an integer") from exc
    isolation = str(data.get("isolation") or "worktree")
    if isolation not in ("worktree", "clone"):
        raise ManifestError("isolation must be worktree or clone")
    graph = str(data.get("graph") or "").strip()
    if not graph:
        raise ManifestError("manifest graph is required")
    milestones = []
    for item in data.get("milestones") or []:
        if not isinstance(item, dict) or not item.get("id"):
            raise ManifestError("each milestone needs an id")
        milestones.append(Milestone(
            id=str(item["id"]),
            name=str(item.get("name") or item["id"]),
            gates=_gates(item.get("gates")),
            on_pass=str(item.get("on_pass") or ""),
        ))
    return Manifest(
        version=1,
        integration_branch=str(data.get("integration_branch") or "main"),
        model_default=str(models.get("default") or ""),
        model_eval=str(models.get("eval") or ""),
        models_roles={str(k): str(v) for k, v in role_models.items()},
        max_parallel_workers=max(1, parallel),
        graph=graph,
        isolation=isolation,
        milestones=tuple(milestones),
        roles=_roles(data.get("roles")),
        path=path,
        sha=canonical_sha(data),
    )


def load_manifest(path: Path) -> Manifest:
    if not path.is_file():
        raise ManifestError(f"manifest not found: {path}")
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ManifestError(f"manifest is not JSON: {exc}") from exc
    return parse_manifest(data, path)


def allowlist_errors(manifest: Manifest, allow: "set[str]") -> list[str]:
    """Role and eval models must be a subset of the effective allowlist."""
    return [
        f"{model} is not on the company allowlist"
        for model in manifest.model_ids()
        if model not in allow
    ]

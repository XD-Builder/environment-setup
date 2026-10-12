"""Append-only JSONL logs for until and graph workflow runs (leaf)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .config import project_dir, utc_now
from . import memory

META_ROLE = "meta"
PAUSE_ROLE = "pause"
APPROVE_ROLE = "approve"
DONE_ROLES = frozenset({"mine", "done"})


def until_dir(slug: "str | None" = None) -> Path:
    d = project_dir(slug) / "until"
    d.mkdir(parents=True, exist_ok=True)
    return d


def all_until_runs(slug: "str | None" = None) -> list[Path]:
    d = project_dir(slug) / "until"
    if not d.exists():
        return []
    return sorted(d.glob("*.jsonl"))


def graphs_dir(slug: "str | None" = None) -> Path:
    d = project_dir(slug) / "graphs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def all_graph_runs(slug: "str | None" = None) -> list[Path]:
    d = project_dir(slug) / "graphs"
    if not d.exists():
        return []
    return sorted(d.glob("*/*.jsonl"))


@dataclass
class UntilRun:
    """Append-only until-run log. Transitions live in ``until_runner``."""

    path: Path
    goal: str
    check_cmd: "str | None" = None
    events: list = field(default_factory=list)

    @classmethod
    def open_new(
        cls,
        path: Path,
        *,
        goal: str,
        check_cmd: "str | None",
        meta_checks: list,
        explicit: bool,
        until_control: "str | None" = None,
        cfg: "dict | None" = None,
        workspace_root: "Path | None" = None,
    ) -> "UntilRun":
        run = cls(path=path, goal=goal, check_cmd=check_cmd or None, events=[])
        meta: dict = {
            "ts": utc_now(),
            "role": META_ROLE,
            "goal": goal,
            "check_cmd": check_cmd or "",
            "checks": meta_checks,
            "explicit": explicit,
        }
        if until_control:
            meta["until_control"] = until_control
        if cfg is not None:
            from .harness_bundle import attach_harness_meta

            attach_harness_meta(
                meta, cfg, workspace_root=workspace_root, checks=meta_checks,
            )
        run._write(meta)
        return run

    @classmethod
    def load(cls, path: Path) -> "UntilRun":
        events = memory.read_jsonl(path)
        goal = ""
        check_cmd = None
        if events and events[0].get("role") == META_ROLE:
            goal = events[0].get("goal") or ""
            check_cmd = events[0].get("check_cmd") or None
        return cls(path=path, goal=goal, check_cmd=check_cmd, events=events)

    def _write(self, row: dict) -> None:
        memory.append_jsonl(self.path, row)
        self.events.append(row)

    def append(self, role: str, status: str, handoff: str = "",
               session: str = "", checks=None, results=None,
               snapshot_ref: str = "", **extra: object) -> None:
        step = sum(1 for e in self.events if e.get("role") not in (META_ROLE,))
        row = {
            "ts": utc_now(),
            "step": step,
            "role": role,
            "status": status,
            "handoff": handoff,
            "session": session,
        }
        if checks is not None:
            row["checks"] = checks
        if results is not None:
            row["results"] = results
        if snapshot_ref:
            row["snapshot_ref"] = snapshot_ref
        for key, val in extra.items():
            if val is not None and val != "" and val != ():
                row[key] = val
        self._write(row)

    def last_work(self) -> "dict | None":
        for ev in reversed(self.events):
            if ev.get("role") not in (META_ROLE, PAUSE_ROLE):
                return ev
        return None

    def is_paused(self) -> bool:
        if not self.events:
            return False
        return self.events[-1].get("role") == PAUSE_ROLE

    def is_done(self) -> bool:
        last = self.last_work()
        if last is None:
            return False
        if last.get("role") in DONE_ROLES:
            return True
        if last.get("role") == "abstain":
            return True
        return last.get("role") == "gate" and last.get("status") == "no"

    def last_handoff(self) -> str:
        for ev in reversed(self.events):
            if ev.get("role") in ("maker", "eval"):
                text = ev.get("handoff") or ""
                if text:
                    return text
        return ""

    def last_maker_handoff(self) -> str:
        for ev in reversed(self.events):
            if ev.get("role") == "maker":
                return ev.get("handoff") or ""
        return ""

    def approved_commands(self) -> list[str]:
        last = self.last_work()
        if not last or last.get("role") != APPROVE_ROLE:
            return []
        try:
            rows = json.loads(last.get("handoff") or "[]")
        except ValueError:
            return []
        return [str(c) for c in rows if isinstance(c, str) and c]

    def session_paths(self) -> list[Path]:
        seen: list[Path] = []
        have: set[str] = set()
        for ev in self.events:
            raw = ev.get("session") or ""
            if not raw or raw in have:
                continue
            have.add(raw)
            seen.append(Path(raw))
        return seen


@dataclass
class GraphRun:
    """Append-only graph-run log. Frontier logic lives in ``graph_runner``."""

    path: Path
    name: str
    events: list = field(default_factory=list)

    @classmethod
    def create(
        cls,
        name: str,
        slug: "str | None" = None,
        *,
        cfg: "dict | None" = None,
        workspace_root: "Path | None" = None,
    ) -> "GraphRun":
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        d = graphs_dir(slug) / name
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{ts}.jsonl"
        n = 1
        while path.exists():
            path = d / f"{ts}-{n}.jsonl"
            n += 1
        run = cls(path=path, name=name, events=[])
        meta: dict = {"ts": utc_now(), "role": META_ROLE, "name": name}
        if cfg is not None:
            from .harness_bundle import attach_harness_meta

            attach_harness_meta(
                meta, cfg, workspace_root=workspace_root, graph_name=name,
            )
        run._write(meta)
        return run

    @classmethod
    def load(cls, path: Path) -> "GraphRun":
        events = memory.read_jsonl(path)
        name = ""
        if events and events[0].get("role") == META_ROLE:
            name = events[0].get("name") or ""
        if not name:
            name = path.parent.name
        return cls(path=path, name=name, events=events)

    def _write(self, row: dict) -> None:
        memory.append_jsonl(self.path, row)
        self.events.append(row)

    def append(self, role: str, status: str, node: str = "",
               handoff: str = "", session: str = "",
               until_run: str = "", verify=None, snapshot_ref: str = "",
               **extra: object) -> None:
        step = sum(1 for e in self.events if e.get("role") not in (META_ROLE,))
        row = {
            "ts": utc_now(),
            "step": step,
            "role": role,
            "status": status,
            "node": node,
            "handoff": handoff,
            "session": session,
            "until_run": until_run,
        }
        if verify:
            row["verify"] = verify
        if snapshot_ref:
            row["snapshot_ref"] = snapshot_ref
        for key, val in extra.items():
            if val is not None and val != "" and val != () and val != []:
                row[key] = val
        self._write(row)

    def last_work(self) -> "dict | None":
        for ev in reversed(self.events):
            if ev.get("role") not in (META_ROLE, PAUSE_ROLE):
                return ev
        return None

    def last_pause(self) -> "dict | None":
        if self.events and self.events[-1].get("role") == PAUSE_ROLE:
            return self.events[-1]
        return None

    def is_paused(self) -> bool:
        if not self.events:
            return False
        return self.events[-1].get("role") == PAUSE_ROLE

    def is_done(self) -> bool:
        last = self.last_work()
        if last is None:
            return False
        if last.get("role") in DONE_ROLES:
            return True
        return last.get("role") == "gate" and last.get("status") == "no"

    def last_handoff(self) -> str:
        for ev in reversed(self.events):
            if ev.get("handoff"):
                return ev.get("handoff") or ""
        return ""

    def handoff_for_node(self, node_name: str) -> str:
        for ev in reversed(self.events):
            if ev.get("role") == "node" and ev.get("node") == node_name:
                return ev.get("handoff") or ""
        return ""

    def session_paths(self) -> list[Path]:
        seen: list[Path] = []
        have: set[str] = set()

        def add(raw: str) -> None:
            if not raw or raw in have:
                return
            have.add(raw)
            seen.append(Path(raw))

        for ev in self.events:
            until_raw = ev.get("until_run") or ""
            if until_raw:
                until_path = Path(until_raw)
                if until_path.is_file():
                    for p in UntilRun.load(until_path).session_paths():
                        add(str(p))
                    continue
            add(ev.get("session") or "")
        return seen

    def resume_until(self, node_name: str) -> "str | None":
        pause = self.last_pause()
        if pause and pause.get("node") == node_name:
            return pause.get("until_run") or None
        return None


def latest_open_until_run(slug: "str | None" = None) -> "UntilRun | None":
    for path in reversed(all_until_runs(slug=slug)):
        run = UntilRun.load(path)
        if run.goal and not run.is_done():
            return run
    return None


def superseded_until_hint(slug: "str | None" = None) -> "str | None":
    prior = latest_open_until_run(slug)
    if prior is None:
        return None
    return (
        f"[until · previous run {prior.path.name} still open; "
        "resume uses the latest run]"
    )


def latest_open_graph_run(slug: "str | None" = None) -> "GraphRun | None":
    for path in reversed(all_graph_runs(slug=slug)):
        run = GraphRun.load(path)
        if not run.is_done():
            return run
    return None


def superseded_graph_hint(slug: "str | None" = None) -> "str | None":
    prior = latest_open_graph_run(slug)
    if prior is None:
        return None
    return (
        f"[graph · previous run {prior.path.name} still open; "
        "resume uses the latest run]"
    )

"""Long-horizon campaigns: plan memory, board, and reflect ticks.

Workers never append this store. The host orchestrator and ``/campaign`` do.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import memory
from .config import cfg_bool, cfg_int, cfg_str, utc_now

PLAN_MAX_CHARS = 4000
BOARD_CLIP = 800
BOARD_TYPES = frozenset({"status", "blocker", "decision_request", "ack", "spawn"})
PATCH_FIELDS = frozenset({"next_actions", "open_questions", "risks", "objective"})
POISON_PHRASES = (
    "ignore previous",
    "ignore the seed",
    "ignore tests",
    "<script",
)


class CampaignError(ValueError):
    """A campaign id, plan patch, or lifecycle gate was rejected."""


class PatchError(CampaignError):
    """A reflect patch failed schema checks. The plan is unchanged."""


def _root(campaign_id: str) -> Path:
    if not campaign_id or "/" in campaign_id or campaign_id.startswith("."):
        raise CampaignError(f"refusing campaign id {campaign_id!r}")
    path = memory.project_dir() / "campaigns" / campaign_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _meta_path(campaign_id: str) -> Path:
    return _root(campaign_id) / "meta.json"


def load_meta(campaign_id: str) -> dict:
    path = _meta_path(campaign_id)
    if not path.is_file():
        raise CampaignError(f"no campaign {campaign_id}")
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise CampaignError("campaign meta is not an object")
    return data


def save_meta(campaign_id: str, meta: dict) -> None:
    _meta_path(campaign_id).write_text(json.dumps(meta, indent=2) + "\n")


def _append(campaign_id: str, name: str, row: dict) -> None:
    memory.append_jsonl(_root(campaign_id) / name, row)


def _rows(campaign_id: str, name: str) -> list:
    path = _root(campaign_id) / name
    if not path.is_file():
        return []
    return memory.read_jsonl(path)


def campaign_id_from_goal(goal: str, now: "str | None" = None) -> str:
    chars = []
    for ch in (goal or "").lower():
        if ch.isalnum():
            chars.append(ch)
        elif chars and chars[-1] != "-":
            chars.append("-")
    base = "".join(chars).strip("-")[:40] or "campaign"
    stamp = (now or utc_now()).replace("-", "").replace(":", "")[:8]
    return f"{base}-{stamp}"


def start(goal: str, campaign_id: str = "", *, company: bool = False,
          workspace: "Path | None" = None, manifest_sha: str = "") -> dict:
    text = (goal or "").strip()
    if not text:
        raise CampaignError("campaign goal is required")
    cid = campaign_id or campaign_id_from_goal(text)
    if (_root(cid) / "meta.json").is_file():
        raise CampaignError(f"campaign {cid} already exists")
    meta = {
        "id": cid,
        "goal": text,
        "created_at": utc_now(),
        "status": "open",
        "revision": 0,
        "manifest_sha": manifest_sha,
        "mode": "company" if company else "single",
        "last_reflect_date": "",
        "paused_until": "",
        "extended_through": "",
    }
    save_meta(cid, meta)
    _append(cid, "plan.jsonl", {
        "ts": utc_now(), "type": "phase", "revision": 0,
        "phase": "M1", "objective": text, "acceptance": [],
        "next_actions": [{"text": text, "role": ""}],
        "open_questions": [], "risks": [],
    })
    _write_mirror(workspace, meta, _rows(cid, "plan.jsonl"))
    return meta


def latest_open() -> "str | None":
    root = memory.project_dir() / "campaigns"
    if not root.is_dir():
        return None
    found = []
    for path in sorted(root.iterdir()):
        meta_path = path / "meta.json"
        if not meta_path.is_file():
            continue
        try:
            meta = json.loads(meta_path.read_text())
        except json.JSONDecodeError:
            continue
        if isinstance(meta, dict) and meta.get("status") in ("open", "paused"):
            found.append((meta.get("created_at") or "", path.name))
    if not found:
        return None
    found.sort()
    return found[-1][1]


def revision_of(campaign_id: str) -> "int | None":
    path = memory.project_dir() / "campaigns" / campaign_id / "meta.json"
    if not path.is_file():
        return None
    try:
        meta = json.loads(path.read_text())
    except json.JSONDecodeError:
        return None
    if not isinstance(meta, dict):
        return None
    return int(meta.get("revision") or 0)


def _latest_typed(rows: list, kind: str) -> "dict | None":
    found = None
    for row in rows:
        if row.get("type") == kind:
            found = row
    return found


def plan_view(campaign_id: str) -> dict:
    rows = _rows(campaign_id, "plan.jsonl")
    phase = _latest_typed(rows, "phase") or {}
    patch = _latest_typed(rows, "patch") or {}
    return {
        "phase": phase.get("phase") or phase.get("objective") or "",
        "objective": patch.get("objective") or phase.get("objective") or "",
        "next_actions": patch.get("next_actions") or phase.get("next_actions") or [],
        "open_questions": patch.get("open_questions") if patch else (phase.get("open_questions") or []),
        "risks": patch.get("risks") if patch else (phase.get("risks") or []),
        "revision": revision_of(campaign_id) or 0,
    }


def plan_block(campaign_id: str, role: str = "", max_chars: int = PLAN_MAX_CHARS) -> str:
    view = plan_view(campaign_id)
    actions = []
    for item in view["next_actions"]:
        if isinstance(item, str):
            actions.append(item)
            continue
        if not isinstance(item, dict):
            continue
        owner = str(item.get("role") or "")
        if owner and role and owner != role:
            continue
        actions.append(str(item.get("text") or ""))
    lines = [
        f"phase: {view['phase']}",
        f"objective: {view['objective']}",
        f"revision: {view['revision']}",
        "next:",
    ]
    lines.extend(f"- {text}" for text in actions if text)
    if view["open_questions"]:
        lines.append("open questions:")
        lines.extend(f"- {item}" for item in view["open_questions"])
    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n"


def _poisoned(text: str) -> bool:
    low = text.lower()
    return any(phrase in low for phrase in POISON_PHRASES)


def _check_strings(value, limit: int) -> None:
    if isinstance(value, str):
        if len(value) > limit or _poisoned(value):
            raise PatchError("patch text is too long or not allowed")
        return
    if isinstance(value, list):
        for item in value:
            _check_strings(item, limit)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in ("web", "url", "raw", "html"):
                raise PatchError("patch must not carry raw web fields")
            _check_strings(item, limit)


def validate_patch(patch: dict) -> None:
    if not isinstance(patch, dict):
        raise PatchError("patch must be an object")
    extra = set(patch) - PATCH_FIELDS
    if extra:
        raise PatchError("unknown patch fields: " + ", ".join(sorted(extra)))
    actions = patch.get("next_actions", [])
    if not isinstance(actions, list):
        raise PatchError("next_actions must be a list")
    for item in actions:
        if isinstance(item, str):
            continue
        if not isinstance(item, dict) or "text" not in item:
            raise PatchError("each next action needs text")
    questions = patch.get("open_questions", [])
    risks = patch.get("risks", [])
    if not isinstance(questions, list) or not isinstance(risks, list):
        raise PatchError("open_questions and risks must be lists")
    _check_strings(patch, 400)
    if len(json.dumps(patch)) > 8000:
        raise PatchError("patch is too large")


def coerce_patch(raw) -> dict:
    if isinstance(raw, dict):
        validate_patch(raw)
        return raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PatchError(f"patch is not JSON: {exc}") from exc
        validate_patch(data)
        return data
    raise PatchError("patch must be a JSON object")


def reflect(campaign_id: str, kind: str, observations: dict, *,
            patch=None, synthesizer=None, workspace: "Path | None" = None,
            now: "datetime | None" = None) -> dict:
    """Append a tick. An invalid patch leaves the plan unchanged."""
    if kind not in ("run_end", "daily", "milestone", "handoff", "blocked"):
        raise CampaignError(f"unknown tick {kind}")
    meta = load_meta(campaign_id)
    current = now or datetime.now(timezone.utc)
    produced = patch
    if produced is None and synthesizer is not None and kind in ("daily", "milestone", "blocked"):
        produced = synthesizer(observations, plan_view(campaign_id))
    applied = False
    error = ""
    if produced is not None:
        try:
            clean = coerce_patch(produced)
        except PatchError as exc:
            error = str(exc)
            clean = None
        if clean is not None:
            meta["revision"] = int(meta.get("revision") or 0) + 1
            _append(campaign_id, "plan.jsonl", {
                "ts": utc_now(), "type": "patch", "revision": meta["revision"],
                **clean,
            })
            applied = True
    if kind == "daily":
        day = current.strftime("%Y-%m-%d")
        meta["last_reflect_date"] = day
        daily = _root(campaign_id) / "daily"
        daily.mkdir(parents=True, exist_ok=True)
        (daily / f"{day}.md").write_text(_digest(meta, observations, applied, error))
    tick = {
        "ts": utc_now(), "type": kind, "status": "fail" if error else "pass",
        "applied": applied, "error": error,
    }
    _append(campaign_id, "ticks.jsonl", tick)
    save_meta(campaign_id, meta)
    _write_mirror(workspace, meta, _rows(campaign_id, "plan.jsonl"))
    return tick


def _digest(meta: dict, observations: dict, applied: bool, error: str) -> str:
    lines = [
        f"# {meta.get('id')} {utc_now()[:10]}",
        "",
        meta.get("goal") or "",
        "",
        f"patch applied: {applied}",
    ]
    if error:
        lines.append(f"patch error: {error}")
    if observations:
        lines.append("")
        lines.append(json.dumps(observations, indent=2)[:2000])
    return "\n".join(lines) + "\n"


def append_board(campaign_id: str, kind: str, *, role: str = "", text: str = "",
                 ref: str = "") -> dict:
    if kind not in BOARD_TYPES:
        raise CampaignError(f"unknown board type {kind}")
    row = {
        "ts": utc_now(), "type": kind, "role": role, "text": (text or "")[:800], "ref": ref,
    }
    _append(campaign_id, "board.jsonl", row)
    return row


def ack_blocker(campaign_id: str, ref: str, text: str = "") -> dict:
    return append_board(campaign_id, "ack", ref=ref, text=text or "cleared")


def board_rows(campaign_id: str) -> list:
    return _rows(campaign_id, "board.jsonl")


def board_text(campaign_id: str) -> str:
    rows = board_rows(campaign_id)
    if not rows:
        return "(board empty)"
    return "\n".join(
        f"{row.get('ts')} {row.get('type')} {row.get('role')}: {row.get('text')}"
        for row in rows
    )


def board_clip(campaign_id: str, role: str = "", limit: int = BOARD_CLIP) -> str:
    rows = board_rows(campaign_id)
    ticks = _rows(campaign_id, "ticks.jsonl")
    since = ticks[-1].get("ts") if ticks else ""
    lines = []
    for row in rows:
        if since and (row.get("ts") or "") < since:
            continue
        owner = str(row.get("role") or "")
        kind = row.get("type")
        if kind == "status" and owner and role and owner != role:
            continue
        lines.append(f"{kind} {owner}: {row.get('text') or ''}".strip())
    text = "\n".join(lines)
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n"


def attach_run(campaign_id: str, *, kind: str, path: str) -> None:
    _append(campaign_id, "runs.jsonl", {"ts": utc_now(), "kind": kind, "path": path})


def defer_work(campaign_id: str, reason: str, day: str = "") -> None:
    _append(campaign_id, "plan.jsonl", {
        "ts": utc_now(), "type": "defer", "reason": reason, "until": day,
    })


def decide_question(campaign_id: str, question: str, decision: str) -> None:
    _append(campaign_id, "plan.jsonl", {
        "ts": utc_now(), "type": "decision", "question": question, "decision": decision,
    })


def status_text(campaign_id: str) -> str:
    meta = load_meta(campaign_id)
    view = plan_view(campaign_id)
    blockers = [row for row in board_rows(campaign_id) if row.get("type") == "blocker"]
    lines = [
        f"{meta['id']} {meta.get('status')} rev {meta.get('revision')}",
        f"goal: {meta.get('goal')}",
        f"phase: {view['phase']}",
        f"objective: {view['objective']}",
    ]
    for item in view["next_actions"]:
        if isinstance(item, dict):
            lines.append(f"next: {item.get('text')}")
        else:
            lines.append(f"next: {item}")
    if blockers:
        lines.append(f"blockers: {len(blockers)}")
    return "\n".join(lines)


def _today(now: "datetime | None") -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def end_of_day_reached(cfg: dict, now: "datetime | None" = None) -> bool:
    raw = cfg_str(cfg, "campaign_end_of_day_utc").strip()
    if not raw:
        return False
    return _today(now).hour >= int(raw)


def age_days(meta: dict, now: "datetime | None" = None) -> int:
    try:
        created = datetime.strptime(meta["created_at"][:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (KeyError, ValueError):
        return 0
    return (_today(now).date() - created.date()).days


def extend(campaign_id: str, cfg: dict, now: "datetime | None" = None) -> dict:
    meta = load_meta(campaign_id)
    today = _today(now).date()
    extra = max(1, cfg_int(cfg, "campaign_max_days"))
    meta["extended_through"] = (today + timedelta(days=extra)).isoformat()
    meta["status"] = "open"
    save_meta(campaign_id, meta)
    return meta


def manifest_refresh(campaign_id: str, manifest_sha: str) -> dict:
    if not manifest_sha:
        raise CampaignError("manifest_sha is required")
    meta = load_meta(campaign_id)
    meta["manifest_sha"] = manifest_sha
    save_meta(campaign_id, meta)
    _append(campaign_id, "ticks.jsonl", {
        "ts": utc_now(), "type": "manifest-refresh", "status": "pass",
        "manifest_sha": manifest_sha,
    })
    return meta


def prepare_resume(cfg: dict, campaign_id: str = "", *,
                   now: "datetime | None" = None,
                   synthesizer=None,
                   workspace: "Path | None" = None) -> dict:
    """Apply daily, end-of-day, and max-day gates. Does not spawn workers."""
    cid = campaign_id or latest_open()
    if not cid:
        raise CampaignError("no open campaign")
    meta = load_meta(cid)
    current = _today(now)
    if age_days(meta, current) >= cfg_int(cfg, "campaign_max_days"):
        extended = str(meta.get("extended_through") or "")
        if not extended or extended < current.date().isoformat():
            meta["status"] = "paused"
            save_meta(cid, meta)
            return {
                "ok": False, "id": cid, "status": "paused",
                "message": "campaign_max_days reached; lmloop campaign extend",
            }
    paused_until = str(meta.get("paused_until") or "")
    if paused_until and paused_until <= current.date().isoformat():
        meta["paused_until"] = ""
        meta["status"] = "open"
        save_meta(cid, meta)
    tick = None
    if cfg_bool(cfg, "campaign_daily_reflect"):
        if meta.get("last_reflect_date") != current.date().isoformat():
            tick = reflect(
                cid, "daily", {"goal": meta.get("goal")},
                synthesizer=synthesizer, workspace=workspace, now=current,
            )
            meta = load_meta(cid)
    if end_of_day_reached(cfg, current):
        tomorrow = (current.date() + timedelta(days=1)).isoformat()
        meta["status"] = "paused"
        meta["paused_until"] = tomorrow
        save_meta(cid, meta)
        return {
            "ok": False, "id": cid, "status": "paused", "tick": tick,
            "message": f"paused until {tomorrow} (end of day)",
        }
    if meta.get("status") == "paused" and not meta.get("paused_until"):
        meta["status"] = "open"
        save_meta(cid, meta)
    return {"ok": True, "id": cid, "status": meta.get("status"), "tick": tick, "message": status_text(cid)}


def mark_done(campaign_id: str) -> dict:
    meta = load_meta(campaign_id)
    meta["status"] = "done"
    save_meta(campaign_id, meta)
    return meta


def _write_mirror(workspace: "Path | None", meta: dict, rows: list) -> None:
    if workspace is None:
        return
    view_phase = ""
    for row in rows:
        if row.get("type") in ("phase", "patch"):
            view_phase = str(row.get("objective") or row.get("phase") or view_phase)
    path = workspace / ".lmloop" / "campaign.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"# Campaign {meta.get('id')}\n\n{meta.get('goal')}\n\n{view_phase}\n"
    )

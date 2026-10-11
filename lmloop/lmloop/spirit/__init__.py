"""Project spirit: actions, thoughts, traits, and a slow self document.

Seed wins over self for values. Actions win over thoughts for facts. Automation
never deletes learnings or overwrites a seed the user edited.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .. import memory
from ..config import cfg_bool, utc_now

SEED_FILE = Path(__file__).with_name("seed.md")
SELF_MAX_CHARS = 4000
OPEN_THOUGHTS = 3
TRAIT_TOP = 5
THOUGHT_KINDS = frozenset({"lesson", "hypothesis", "style", "meta"})
THOUGHT_STATUS = frozenset({"open", "promoted", "rejected", "superseded"})
POISON_PHRASES = (
    "ignore tests",
    "ignore the seed",
    "ignore previous",
    "override safety",
    "disregard the charter",
)
_SECRET_PARTS = ("token", "secret", "password", "authorization", "api_key")
_CORRECTION_WORDS = frozenset({"no", "wrong", "don't", "dont", "undo", "stop"})
_ALWAYS_LOG = frozenset({
    "remember", "log_decision", "reflect_thought",
    "write_file", "delete_file", "update_file",
})
_KEEP_SECTIONS = ("## Role in this repo", "## Heuristics")
_EXPERTISE = "## Expertise map"


class SpiritError(ValueError):
    """A distill patch or thought was rejected."""


def spirit_dir(slug: "str | None" = None, *, create: bool = True) -> Path:
    path = memory.project_dir(slug) / "spirit"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def _read_rows(name: str, slug: "str | None" = None) -> list:
    path = spirit_dir(slug, create=False) / name
    if not path.is_file():
        return []
    return memory.read_jsonl(path)


def _append(name: str, row: dict, slug: "str | None" = None) -> dict:
    memory.append_jsonl(spirit_dir(slug) / name, row)
    return row


def packaged_seed() -> str:
    return SEED_FILE.read_text()


def seed_path(slug: "str | None" = None, workspace: "Path | None" = None) -> "Path | None":
    if workspace is not None:
        override = workspace / ".lmloop" / "spirit" / "seed.md"
        if override.is_file():
            return override
    copy = spirit_dir(slug, create=False) / "seed.md"
    if copy.is_file():
        return copy
    return None


def read_seed(slug: "str | None" = None, workspace: "Path | None" = None) -> str:
    path = seed_path(slug, workspace)
    if path is not None:
        return path.read_text()
    return packaged_seed()


def ensure_seed(slug: "str | None" = None) -> Path:
    """Copy the packaged charter once. Never replace a seed that already exists."""
    dest = spirit_dir(slug) / "seed.md"
    if not dest.exists():
        dest.write_text(packaged_seed())
    return dest


def poisoned(text: str) -> bool:
    low = (text or "").lower()
    return any(phrase in low for phrase in POISON_PHRASES)


def _worker_blocked(cfg: dict) -> bool:
    from ..company.allowlist import worker_memory_blocked
    return worker_memory_blocked(cfg)


def _link(cfg: dict, typ: str, key: str, label: str) -> None:
    if not cfg_bool(cfg, "use_graph") or _worker_blocked(cfg):
        return
    from .. import knowledge_graph
    knowledge_graph.add_graph_node(typ, key, label=label[:200], source="observed")


def redact_args(args) -> dict:
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
        except json.JSONDecodeError:
            return {"_raw": args[:200]}
    else:
        parsed = args
    if not isinstance(parsed, dict):
        return {"_raw": str(parsed)[:200]}
    out = {}
    for key, value in parsed.items():
        low = key.lower()
        if any(part in low for part in _SECRET_PARTS):
            out[key] = "[redacted]"
        elif isinstance(value, str):
            out[key] = value[:200]
        else:
            out[key] = value
    return out


def _should_log(name: str, ok: bool, args: dict) -> bool:
    if not ok or name in _ALWAYS_LOG:
        return True
    if name == "run_shell":
        command = str(args.get("command") or args.get("_raw") or "")
        return len(command) <= 120
    return False


def note_tool(cfg: dict, *, name: str, args, result: str, session_log=None, ok: bool = True) -> "dict | None":
    if not cfg_bool(cfg, "use_spirit") or not cfg_bool(cfg, "spirit_actions"):
        return None
    if _worker_blocked(cfg):
        return None
    redacted = redact_args(args)
    if not _should_log(name, ok, redacted):
        return None
    row = _append("actions.jsonl", {
        "id": "act_" + uuid.uuid4().hex[:8],
        "ts": utc_now(),
        "session_id": str(session_log or ""),
        "tool": name,
        "args_redacted": redacted,
        "exit_ok": bool(ok),
        "summary_line": (result or "")[:240],
    })
    _link(cfg, "action", row["id"], f"{name} {'ok' if ok else 'fail'}")
    return row


def looks_like_correction(text: str) -> bool:
    tokens = []
    for raw in (text or "").split():
        tokens.append(raw.strip(".,!?;:\"'").lower())
    return bool(tokens) and tokens[0] in _CORRECTION_WORDS


def note_user_text(cfg: dict, text: str, session_log=None) -> "dict | None":
    if not looks_like_correction(text):
        return None
    return note_tool(
        cfg, name="user_correction", args={"text": (text or "")[:200]},
        result=(text or "")[:240], session_log=session_log, ok=False,
    )


def add_thought(kind: str, text: str, *, evidence: "list | None" = None,
                cfg: "dict | None" = None, slug: "str | None" = None) -> dict:
    if kind not in THOUGHT_KINDS:
        raise SpiritError(f"thought kind must be one of {sorted(THOUGHT_KINDS)}")
    body = (text or "").strip()
    if not body:
        raise SpiritError("thought text is empty")
    if poisoned(body):
        raise SpiritError("thought contradicts the seed charter")
    row = _append("thoughts.jsonl", {
        "id": "th_" + uuid.uuid4().hex[:8],
        "ts": utc_now(),
        "kind": kind,
        "text": body,
        "evidence": list(evidence or []),
        "status": "open",
        "links": {},
    }, slug)
    if cfg is not None:
        _link(cfg, "thought", row["id"], body[:120])
    return row


def latest_thoughts(slug: "str | None" = None) -> dict:
    found: dict = {}
    for row in _read_rows("thoughts.jsonl", slug):
        if row.get("id"):
            found[row["id"]] = row
    return found


def set_thought_status(thought_id: str, status: str, slug: "str | None" = None) -> dict:
    if status not in THOUGHT_STATUS:
        raise SpiritError(f"unknown thought status {status!r}")
    prior = latest_thoughts(slug).get(thought_id)
    if prior is None:
        raise SpiritError(f"unknown thought {thought_id}")
    row = dict(prior)
    row["ts"] = utc_now()
    row["status"] = status
    return _append("thoughts.jsonl", row, slug)


def open_thoughts(slug: "str | None" = None, limit: int = OPEN_THOUGHTS) -> list:
    rows = [row for row in latest_thoughts(slug).values() if row.get("status") == "open"]
    rows.sort(key=lambda row: row.get("ts") or "")
    return rows[-limit:]


def add_trait(text: str, *, strength: int = 5, source: str = "synthesized",
              trait_id: str = "", cfg: "dict | None" = None,
              slug: "str | None" = None) -> dict:
    body = (text or "").strip()
    if not body:
        raise SpiritError("trait text is empty")
    if poisoned(body):
        raise SpiritError("trait contradicts the seed charter")
    if source not in ("user-stated", "observed", "synthesized"):
        raise SpiritError("trait source must be user-stated, observed, or synthesized")
    row = _append("traits.jsonl", {
        "id": trait_id or ("tr_" + uuid.uuid4().hex[:8]),
        "ts": utc_now(),
        "text": body,
        "strength": max(0, min(10, int(strength))),
        "source": source,
        "event": "set",
    }, slug)
    if cfg is not None:
        _link(cfg, "trait", row["id"], body[:120])
    return row


def supersede_trait(trait_id: str, slug: "str | None" = None) -> dict:
    return _append("traits.jsonl", {
        "id": trait_id, "ts": utc_now(), "text": "", "strength": 0,
        "source": "synthesized", "event": "supersede",
    }, slug)


def _age_days(ts: str, now: "datetime | None" = None) -> float:
    try:
        then = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return 0.0
    current = now or datetime.now(timezone.utc)
    return max(0.0, (current - then).total_seconds() / 86400.0)


def active_traits(slug: "str | None" = None, now: "datetime | None" = None) -> list:
    latest: dict = {}
    for row in _read_rows("traits.jsonl", slug):
        if row.get("id"):
            latest[row["id"]] = row
    live = []
    for row in latest.values():
        if row.get("event") == "supersede":
            continue
        strength = int(row.get("strength") or 0)
        if row.get("source") != "user-stated":
            strength = max(0, strength - int(_age_days(row.get("ts") or "", now) // 30))
        if strength <= 0:
            continue
        item = dict(row)
        item["strength"] = strength
        live.append(item)
    live.sort(key=lambda row: (-int(row["strength"]), row.get("ts") or ""))
    return live


def write_self(text: str, slug: "str | None" = None, cfg: "dict | None" = None) -> Path:
    body = (text or "").strip()
    if not body:
        raise SpiritError("self.md is empty")
    if poisoned(body):
        raise SpiritError("self.md contradicts the seed charter")
    root = spirit_dir(slug)
    dest = root / "self.md"
    if dest.is_file():
        history = root / "self_history"
        history.mkdir(parents=True, exist_ok=True)
        (history / f"{utc_now().replace(':', '')}.md").write_text(dest.read_text())
    dest.write_text(body if body.endswith("\n") else body + "\n")
    if cfg is not None:
        _link(cfg, "self_snapshot", dest.name, "self")
    return dest


def compress_self(text: str, max_chars: int = SELF_MAX_CHARS) -> str:
    """Keep role and heuristics. Cap the expertise map. Drop open questions if needed."""
    raw = text or ""
    if len(raw) <= max_chars:
        return raw
    sections = _sections(raw)
    kept = []
    for heading in _KEEP_SECTIONS:
        if heading in sections:
            kept.append(heading + "\n" + sections[heading].strip())
    expertise = sections.get(_EXPERTISE, "")
    if expertise:
        lines = [line for line in expertise.splitlines() if line.strip()][:5]
        kept.append(_EXPERTISE + "\n" + "\n".join(lines))
    body = "\n\n".join(part for part in kept if part.strip())
    if not body.strip():
        clipped = raw[:max_chars].rstrip()
        return clipped + ("\n" if clipped else "")
    if len(body) <= max_chars:
        return body
    return body[:max_chars].rstrip() + "\n"


def _sections(text: str) -> dict:
    current = ""
    buckets: dict[str, list] = {"": []}
    for line in text.splitlines():
        if line.startswith("## "):
            current = line.strip()
            buckets[current] = []
            continue
        buckets.setdefault(current, []).append(line)
    return {key: "\n".join(lines).strip() for key, lines in buckets.items()}


def _remote_hides_self(cfg: dict) -> bool:
    from ..server import base_url_is_local
    if base_url_is_local(cfg):
        return False
    return not cfg_bool(cfg, "spirit_remote")


def spirit_block(cfg: dict, slug: "str | None" = None,
                 workspace: "Path | None" = None) -> str:
    if not cfg_bool(cfg, "use_spirit"):
        return ""
    parts = ["## Spirit", read_seed(slug, workspace).strip()]
    if _remote_hides_self(cfg):
        parts.append("Self narrative omitted on a remote endpoint.")
        return "\n\n".join(parts)
    self_path = spirit_dir(slug, create=False) / "self.md"
    if self_path.is_file():
        parts.append("## Project self\n" + compress_self(self_path.read_text()).strip())
    traits = active_traits(slug)[:TRAIT_TOP]
    if traits:
        lines = [f"- {row['id']}: {row['text']}" for row in traits]
        parts.append("## Traits\n" + "\n".join(lines))
    thoughts = open_thoughts(slug)
    if thoughts:
        lines = [f"- {row['id']}: {row['text']}" for row in thoughts]
        parts.append("## Working reflections\n" + "\n".join(lines))
    parts.append(
        "When a self heuristic changes an action, say `Spirit heuristic applied: <label>`."
    )
    return "\n\n".join(parts)


def log_text(slug: "str | None" = None) -> str:
    actions = _read_rows("actions.jsonl", slug)
    thoughts = list(latest_thoughts(slug).values())
    open_count = sum(1 for row in thoughts if row.get("status") == "open")
    return "\n".join([
        f"actions: {len(actions)}",
        f"thoughts: {len(thoughts)} open {open_count}",
        f"traits: {len(active_traits(slug))}",
        f"self: {'yes' if (spirit_dir(slug, create=False) / 'self.md').is_file() else 'no'}",
    ])


def review_text(slug: "str | None" = None) -> str:
    lines = ["# Spirit review", ""]
    self_path = spirit_dir(slug, create=False) / "self.md"
    if self_path.is_file():
        lines.append(self_path.read_text().rstrip())
        lines.append("")
    lines.append("## Traits")
    traits = active_traits(slug)
    if not traits:
        lines.append("(none)")
    for row in traits:
        lines.append(f"- {row['id']} ({row['source']}, {row['strength']}): {row['text']}")
    lines.append("")
    lines.append("## Open thoughts")
    thoughts = open_thoughts(slug, limit=20)
    if not thoughts:
        lines.append("(none)")
    for row in thoughts:
        lines.append(f"- {row['id']} [{row.get('kind')}]: {row['text']}")
    return "\n".join(lines) + "\n"


def _preview_promote(item: dict) -> None:
    kind = item.get("promote") or "none"
    if kind == "none":
        return
    if kind == "learning":
        insight = str((item.get("learning") or {}).get("insight") or "")
        if not insight.strip() or poisoned(insight):
            raise SpiritError("refusing to promote a learning that contradicts the seed")
        return
    if kind == "decision":
        decision = str((item.get("decision") or {}).get("decision") or "")
        if not decision.strip() or poisoned(decision):
            raise SpiritError("refusing to promote a decision that contradicts the seed")
        return
    if kind == "trait":
        text = str((item.get("trait") or {}).get("text") or "")
        if not text.strip() or poisoned(text):
            raise SpiritError("trait contradicts the seed charter")
        return
    raise SpiritError(f"unknown promote target {kind!r}")


def _promote(item: dict, cfg: dict, slug: "str | None") -> None:
    kind = item.get("promote") or "none"
    if kind == "none":
        return
    if kind == "learning":
        body = item.get("learning") or {}
        insight = str(body.get("insight") or "").strip()
        if not insight or poisoned(insight):
            raise SpiritError("refusing to promote a learning that contradicts the seed")
        memory.add_learning(
            insight, type=str(body.get("type") or "pattern"),
            key=str(body.get("key") or ""), confidence=int(body.get("confidence") or 6),
            cfg=cfg, slug=slug,
        )
        return
    if kind == "decision":
        body = item.get("decision") or {}
        decision = str(body.get("decision") or "").strip()
        if not decision or poisoned(decision):
            raise SpiritError("refusing to promote a decision that contradicts the seed")
        memory.add_decision(
            decision, rationale=str(body.get("rationale") or ""), cfg=cfg, slug=slug,
        )
        return
    if kind == "trait":
        body = item.get("trait") or {}
        add_trait(
            str(body.get("text") or ""), strength=int(body.get("strength") or 5),
            source=str(body.get("source") or "synthesized"),
            trait_id=str(body.get("id") or ""), cfg=cfg, slug=slug,
        )
        return
    raise SpiritError(f"unknown promote target {kind!r}")


def apply_distill(patch: dict, cfg: dict, slug: "str | None" = None) -> dict:
    """Apply a validated distill patch. Learnings are only appended, never deleted."""
    if not isinstance(patch, dict):
        raise SpiritError("distill patch must be an object")
    thoughts = patch.get("thoughts") or []
    if not isinstance(thoughts, list):
        raise SpiritError("distill thoughts must be a list")
    self_md = patch.get("self_md")
    if isinstance(self_md, str) and self_md.strip() and poisoned(self_md):
        raise SpiritError("self.md contradicts the seed charter")
    for item in thoughts:
        if not isinstance(item, dict) or not item.get("id"):
            raise SpiritError("each thought update needs an id")
        status = str(item.get("status") or "open")
        if status not in THOUGHT_STATUS:
            raise SpiritError(f"unknown thought status {status!r}")
        if item["id"] not in latest_thoughts(slug):
            raise SpiritError(f"unknown thought {item['id']}")
        _preview_promote(item)
    applied = []
    for item in thoughts:
        if not isinstance(item, dict) or not item.get("id"):
            raise SpiritError("each thought update needs an id")
        status = str(item.get("status") or "open")
        set_thought_status(str(item["id"]), status, slug)
        _promote(item, cfg, slug)
        applied.append(item["id"])
    wrote = False
    if isinstance(self_md, str) and self_md.strip():
        write_self(self_md, slug, cfg=cfg)
        wrote = True
    return {"thoughts": applied, "self": wrote}


def note_distill_request(cfg: dict, paths) -> "Path | None":
    if not cfg_bool(cfg, "use_spirit") or not cfg_bool(cfg, "spirit_distill_after_mine"):
        return None
    dest = spirit_dir() / "pending_distill.json"
    dest.write_text(json.dumps({
        "ts": utc_now(),
        "sessions": [str(path) for path in paths],
    }, indent=2) + "\n")
    return dest


def search_lines(query: str, slug: "str | None" = None, limit: int = 3) -> list[str]:
    words, idents = memory.tokenize_query(query)
    tokens = [item.lower() for item in words + idents]
    if not tokens:
        return []
    scored = []
    thought_rows = [
        row for row in latest_thoughts(slug).values()
        if row.get("status") in ("open", "promoted")
    ]
    sources = (
        (thought_rows, "thought", "text"),
        (_read_rows("actions.jsonl", slug), "action", "summary_line"),
        (_read_rows("traits.jsonl", slug), "trait", "text"),
    )
    for rows, kind, field in sources:
        for row in rows:
            if row.get("event") == "supersede":
                continue
            body = str(row.get(field) or "")
            low = body.lower()
            hits = sum(1 for token in tokens if token in low)
            if hits:
                scored.append((hits, f"- [{kind} {row.get('id') or ''}] {body[:300]}"))
    scored.sort(key=lambda item: -item[0])
    return [line for _score, line in scored[:limit]]

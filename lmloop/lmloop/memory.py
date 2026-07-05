"""File-only memory: learnings, decisions, session history, checkpoints.

Design rules (borrowed from gstack):
- Append-only writes, computed views at read time (no merge conflicts, no corruption).
- Learnings dedup by ``key`` with latest-wins; confidence decays for non-user-stated
  entries (-1 per 30 days) so stale observations fade out of context.
- Decisions are event-sourced: a ``supersede`` event retires an earlier ``decide``.
- Everything is human-readable JSONL/markdown under ~/.lmloop — no database.
"""

import json
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .config import project_dir

LEARNING_TYPES = ("pattern", "pitfall", "preference", "architecture", "tool", "operational")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _read_jsonl(path: Path) -> "list[dict]":
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


# ---------------------------------------------------------------- learnings

def add_learning(insight: str, type: str = "pattern", key: str = "",
                 confidence: int = 7, source: str = "observed",
                 slug: "str | None" = None) -> dict:
    if type not in LEARNING_TYPES:
        type = "pattern"
    key = re.sub(r"[^a-zA-Z0-9_-]", "-", key or insight[:40].strip().lower().replace(" ", "-"))
    row = {
        "ts": _now(),
        "type": type,
        "key": key,
        "insight": insight.strip(),
        "confidence": max(1, min(10, int(confidence))),
        "source": source if source in ("observed", "user-stated", "inferred") else "observed",
    }
    _append_jsonl(project_dir(slug) / "learnings.jsonl", row)
    return row


def _effective_confidence(row: dict) -> float:
    conf = float(row.get("confidence", 5))
    if row.get("source") == "user-stated":
        return conf
    try:
        ts = datetime.strptime(row["ts"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - ts).days
    except (KeyError, ValueError):
        age_days = 0
    return conf - (age_days / 30.0)


def get_learnings(query: str = "", limit: int = 20, slug: "str | None" = None) -> "list[dict]":
    """Deduped (latest row per key wins), decayed, optionally keyword-filtered."""
    rows = _read_jsonl(project_dir(slug) / "learnings.jsonl")
    by_key: "dict[str, dict]" = {}
    for row in rows:  # file order == chronological, so later rows overwrite
        if row.get("key") and row.get("insight"):
            by_key[row["key"]] = row
    items = [r for r in by_key.values() if _effective_confidence(r) > 0]
    if query:
        terms = [t for t in re.split(r"\W+", query.lower()) if len(t) > 2]
        def score(r: dict) -> int:
            hay = (r.get("key", "") + " " + r.get("insight", "")).lower()
            return sum(1 for t in terms if t in hay)
        items = [r for r in items if score(r) > 0]
        items.sort(key=lambda r: (-score(r), -_effective_confidence(r)))
    else:
        items.sort(key=lambda r: -_effective_confidence(r))
    return items[:limit]


# ---------------------------------------------------------------- decisions

def add_decision(decision: str, rationale: str = "", supersedes: str = "",
                 slug: "str | None" = None) -> dict:
    row = {
        "id": uuid.uuid4().hex[:12],
        "kind": "supersede" if supersedes else "decide",
        "decision": decision.strip(),
        "rationale": rationale.strip(),
        "date": _now(),
    }
    if supersedes:
        row["supersedes"] = supersedes
    _append_jsonl(project_dir(slug) / "decisions.jsonl", row)
    return row


def get_decisions(limit: int = 20, slug: "str | None" = None) -> "list[dict]":
    """Active set: decide/supersede events not retired by a later supersede."""
    rows = _read_jsonl(project_dir(slug) / "decisions.jsonl")
    retired = {r["supersedes"] for r in rows if r.get("supersedes")}
    active = [r for r in rows if r.get("id") not in retired and r.get("decision")]
    return active[-limit:]


# ---------------------------------------------------------------- history

def new_session_log(slug: "str | None" = None) -> Path:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = project_dir(slug) / "sessions" / f"{ts}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def log_event(session_path: Path, role: str, content: str) -> None:
    _append_jsonl(session_path, {"ts": _now(), "role": role, "content": content})


def list_sessions(limit: int = 10, slug: "str | None" = None) -> "list[Path]":
    return all_sessions(slug=slug)[-limit:]


def all_sessions(slug: "str | None" = None) -> "list[Path]":
    d = project_dir(slug) / "sessions"
    if not d.exists():
        return []
    return sorted(d.glob("*.jsonl"))


def read_session(path: Path, max_chars: int = 20000) -> str:
    """Render a session transcript as plain text (most recent complete lines within max_chars)."""
    lines = []
    for row in _read_jsonl(path):
        lines.append(f"[{row.get('role', '?')}] {row.get('content', '')}")
    kept: list = []
    total = 0
    for line in reversed(lines):
        add = len(line) + (1 if kept else 0)
        if total + add > max_chars and kept:
            break
        kept.append(line)
        total += add
    return "\n".join(reversed(kept))


# ---------------------------------------------------------------- checkpoints

def save_checkpoint(title: str, body: str, slug: "str | None" = None) -> Path:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe = re.sub(r"[^a-zA-Z0-9_-]", "-", title.strip().lower())[:50] or "checkpoint"
    path = project_dir(slug) / "checkpoints" / f"{ts}-{safe}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ntitle: {title}\ntimestamp: {_now()}\n---\n\n{body.strip()}\n")
    return path


def latest_checkpoint(slug: "str | None" = None) -> "Path | None":
    d = project_dir(slug) / "checkpoints"
    if not d.exists():
        return None
    files = sorted(d.glob("*.md"))  # filename sort == chronological
    return files[-1] if files else None


def list_checkpoints(limit: int = 15, slug: "str | None" = None) -> "list[Path]":
    return all_checkpoints(slug=slug)[-limit:]


def all_checkpoints(slug: "str | None" = None) -> "list[Path]":
    d = project_dir(slug) / "checkpoints"
    if not d.exists():
        return []
    return sorted(d.glob("*.md"))


def read_checkpoint_body(path: Path) -> str:
    text = path.read_text()
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            return parts[2].strip()
    return text.strip()


def resolve_checkpoint(query: str = "", slug: "str | None" = None) -> "Path | None":
    """Find checkpoint by 'latest', filename stem, or 1-based global index."""
    files = all_checkpoints(slug=slug)
    if not files:
        return None
    q = (query or "latest").strip().lower()
    if q in ("latest", "last", ""):
        return files[-1]
    if q.isdigit():
        idx = int(q) - 1
        if 0 <= idx < len(files):
            return files[idx]
    for path in reversed(files):
        if q in path.stem.lower():
            return path
    return None


def resolve_session(query: str = "", slug: "str | None" = None) -> "Path | None":
    """Find session log by 'latest', stem, or 1-based global index."""
    files = all_sessions(slug=slug)
    if not files:
        return None
    q = (query or "latest").strip().lower()
    if q in ("latest", "last", ""):
        return files[-1]
    if q.isdigit():
        idx = int(q) - 1
        if 0 <= idx < len(files):
            return files[idx]
    for path in reversed(files):
        if q in path.stem.lower():
            return path
    return None


def session_preview(path: Path, max_chars: int = 120) -> str:
    rows = _read_jsonl(path)
    for row in rows:
        if row.get("role") == "user":
            text = (row.get("content") or "").strip().replace("\n", " ")
            if len(text) > max_chars:
                return text[:max_chars] + "..."
            return text or "(empty)"
    return "(no user messages)"


# ---------------------------------------------------------------- context recovery

def context_block(cfg: dict, slug: "str | None" = None) -> str:
    """Bounded memory snapshot injected into the system prompt at session start."""
    parts = []
    decisions = get_decisions(limit=cfg.get("context_decisions", 6), slug=slug)
    if decisions:
        lines = [f"- [{d['id']}] {d['decision']}" + (f" (why: {d['rationale']})" if d.get("rationale") else "")
                 for d in decisions]
        parts.append("Active decisions (treat as settled unless the user reverses them):\n" + "\n".join(lines))
    learnings = get_learnings(limit=cfg.get("context_learnings", 8), slug=slug)
    if learnings:
        lines = [f"- ({r['type']}, {r['confidence']}/10) {r['insight']}" for r in learnings]
        parts.append("Prior learnings from this project:\n" + "\n".join(lines))
    cp = latest_checkpoint(slug=slug)
    if cp:
        age_h = (time.time() - cp.stat().st_mtime) / 3600
        if age_h < 24 * 14:
            parts.append(f"Most recent checkpoint ({age_h:.0f}h ago):\n{cp.read_text()[:2000]}")
    return "\n\n".join(parts)

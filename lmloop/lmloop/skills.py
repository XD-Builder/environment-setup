"""Skill playbooks: filesystem, name validation, and system-prompt assembly.

Packaged markdown lives in ``lmloop/skills/``; user skills in
``~/.lmloop/skills/`` override the same name. This module is the owner —
``agent.py`` only drafts new skills (one-shot chat) and the loop consumes
``system_prompt()``.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from . import memory, steer, tools
from .commands import RESERVED_SKILL_NAMES
from .config import STATE_ROOT

SKILLS_DIR = Path(__file__).parent / "skills"
USER_SKILLS_DIR = STATE_ROOT / "skills"
SKILL_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")

HOW_TO_WORK_MARKER = "\n## How to work"
TOOLS_HEADING = "## Tools available"
CONTEXT_HEADING = "## Context recovery (from project memory)"
MSG_SKILL_NAME_SHAPE = (
    "skill name must be lowercase, start with a letter, and use only a-z 0-9 _ -"
)


def msg_skill_missing(name: str, available: str) -> str:
    return f"No skill '{name}'. Available: {available}"


def msg_skill_reserved(name: str) -> str:
    return f"skill name '{name}' is reserved"


@dataclass
class SkillLibrary:
    """Search order and load/save for packaged + user skill markdown."""

    packaged: Path
    user: Path

    def dirs(self) -> "list[Path]":
        """Search order for loading: user skills override packaged skills."""
        found = []
        if self.user.is_dir():
            found.append(self.user)
        found.append(self.packaged)
        return found

    def is_public(self, stem: str) -> bool:
        """User-facing skills: skip system prompt and private _*.md files."""
        return stem != "system" and not stem.startswith("_")

    def invalid_name(self, name: str) -> bool:
        """Reject empty or path-like names before joining under skills dirs."""
        return (
            not name
            or "/" in name
            or "\\" in name
            or name.startswith(".")
            or ".." in name
        )

    def path(self, name: str) -> "Path | None":
        for d in self.dirs():
            path = d / f"{name}.md"
            if path.is_file():
                return path
        return None

    def list_public(self) -> "list[str]":
        names = set()
        for d in self.dirs():
            for p in d.glob("*.md"):
                if self.is_public(p.stem):
                    names.add(p.stem)
        return sorted(names)

    def load(self, name: str, *, public_only: bool = False) -> str:
        """Load a skill body. When public_only, hide system/_*.md from callers."""
        if self.invalid_name(name) or (public_only and not self.is_public(name)):
            available = ", ".join(self.list_public()) or "(none)"
            raise FileNotFoundError(msg_skill_missing(name, available))
        path = self.path(name)
        if path is None:
            available = ", ".join(self.list_public()) or "(none)"
            raise FileNotFoundError(msg_skill_missing(name, available))
        return path.read_text()

    def blurb(self, name: str) -> str:
        try:
            first = self.load(name).strip().splitlines()[0]
            return first.lstrip("#").strip()[:80]
        except (FileNotFoundError, IndexError):
            return ""

    def save_user(self, name: str, content: str) -> Path:
        """Write a confirmed skill into the user skills directory."""
        err = validate_skill_name(name)
        if err:
            raise ValueError(err)
        self.user.mkdir(parents=True, exist_ok=True)
        path = self.user / f"{name}.md"
        path.write_text(content if content.endswith("\n") else content + "\n")
        return path

    def system_prompt(self, cfg: dict, workspace_root: "Path | None" = None,
                      clock_now=None) -> str:
        base = self.load("system")
        names = tools.tool_names(cfg=cfg)
        tools_block = (
            "\n\n" + TOOLS_HEADING + "\n\n"
            + ", ".join(f"`{n}`" for n in names)
            + ".\n"
        )
        # Inject after the first paragraph / before "## How to work" when present.
        if HOW_TO_WORK_MARKER in base:
            base = base.replace(HOW_TO_WORK_MARKER, tools_block + HOW_TO_WORK_MARKER, 1)
        else:
            base += tools_block
        base += "\n\n" + steer.clock_block(now=clock_now)
        steering = steer.steering_block(workspace_root)
        if steering:
            base += "\n\n" + steering
        ctx = memory.context_block(cfg)
        if ctx:
            base += "\n\n" + CONTEXT_HEADING + "\n\n" + ctx
        return base


def _library() -> SkillLibrary:
    """Fresh library so tests can patch USER_SKILLS_DIR / SKILLS_DIR."""
    return SkillLibrary(packaged=SKILLS_DIR, user=USER_SKILLS_DIR)


def skill_dirs() -> "list[Path]":
    return _library().dirs()


def skill_path(name: str) -> "Path | None":
    return _library().path(name)


def list_skills() -> "list[str]":
    """Skill names from packaged + ~/.lmloop/skills/, excluding system/_author."""
    return _library().list_public()


def load_skill(name: str, *, public_only: bool = False) -> str:
    return _library().load(name, public_only=public_only)


def skill_blurb(name: str) -> str:
    return _library().blurb(name)


def validate_skill_name(name: str) -> "str | None":
    """Return an error message if name is invalid, else None."""
    if not name or not SKILL_NAME_RE.match(name):
        return MSG_SKILL_NAME_SHAPE
    if name in RESERVED_SKILL_NAMES or name.startswith("_"):
        return msg_skill_reserved(name)
    return None


def extract_skill_markdown(text: str) -> str:
    """Normalize model output into a skill markdown body."""
    body = (text or "").strip()
    if body.startswith("```"):
        lines = body.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        body = "\n".join(lines).strip()
    if body and not body.endswith("\n"):
        body += "\n"
    return body


def save_user_skill(name: str, content: str) -> Path:
    """Write a confirmed skill into ~/.lmloop/skills/<name>.md."""
    return _library().save_user(name, content)


def system_prompt(cfg: dict, workspace_root: "Path | None" = None,
                  clock_now=None) -> str:
    return _library().system_prompt(cfg, workspace_root, clock_now=clock_now)

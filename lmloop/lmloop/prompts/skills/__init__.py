"""Skill playbooks: filesystem, name validation, and system-prompt assembly.

Packaged markdown lives in ``lmloop/prompts/skills/``; user skills in
``~/.lmloop/skills/`` override the same name. This module is the owner —
``agent.py`` only drafts new skills (one-shot chat) and the loop consumes
``system_prompt()``.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from ... import memory
from .. import steer
from ...capabilities import tools
from ...core.commands import RESERVED_SKILL_NAMES
from ...core.config import Config, STATE_ROOT

SKILLS_DIR = Path(__file__).parent
USER_SKILLS_DIR = STATE_ROOT / "skills"
SKILL_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")

HOW_TO_WORK_MARKER = "\n## Curiosity"
TOOLS_HEADING = "## Tools available"
CONTEXT_HEADING = "## Context recovery (from project memory)"
SKILL_INDEX_HEADING = "## Skill index"
USE_WHEN_PREFIX = "Use when:"
INDEX_OPT_OUT = "index: no"
MSG_SKILL_NAME_SHAPE = (
    "skill name must be lowercase, start with a letter, and use only a-z 0-9 _ -"
)


def msg_skill_user_starts(name: str) -> str:
    return f"The user starts this with /{name}. Do not load it from read_skill."
_BODY_FILE = "SKILL.md"


def msg_skill_missing(name: str, available: str) -> str:
    return f"No skill '{name}'. Available: {available}"


def msg_skill_reserved(name: str) -> str:
    return f"skill name '{name}' is reserved"


def _valid_part(part: str) -> bool:
    """Sibling file stems use the same charset as skill names."""
    return bool(part) and bool(SKILL_NAME_RE.match(part)) and part != "skill"


def _skill_file(directory: Path, name: str) -> "Path | None":
    """Flat markdown, else ``name/SKILL.md``."""
    flat = directory / f"{name}.md"
    if flat.is_file():
        return flat
    nested = directory / name / _BODY_FILE
    if nested.is_file():
        return nested
    return None


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
        """Flat ``name.md`` wins over ``name/SKILL.md`` in the same directory.

        User directories are searched before packaged ones, so a user file of
        either layout overrides the packaged skill.
        """
        if self.invalid_name(name):
            return None
        for directory in self.dirs():
            found = _skill_file(directory, name)
            if found is not None:
                return found
        return None

    def part_path(self, name: str, part: str) -> "Path | None":
        """Sibling ``name/<part>.md`` next to a flat or directory skill."""
        if self.invalid_name(name) or not _valid_part(part):
            return None
        if self.path(name) is None:
            return None
        for directory in self.dirs():
            candidate = directory / name / f"{part}.md"
            if candidate.is_file():
                return candidate
        return None

    def list_parts(self, name: str) -> "list[str]":
        found = set()
        if self.invalid_name(name):
            return []
        for directory in self.dirs():
            folder = directory / name
            if not folder.is_dir():
                continue
            for path in folder.glob("*.md"):
                if path.name == _BODY_FILE or not self.is_public(path.stem):
                    continue
                found.add(path.stem)
        return sorted(found)

    def list_public(self) -> "list[str]":
        names = set()
        for directory in self.dirs():
            if not directory.is_dir():
                continue
            for path in directory.glob("*.md"):
                if self.is_public(path.stem):
                    names.add(path.stem)
            for child in directory.iterdir():
                if (
                    child.is_dir()
                    and self.is_public(child.name)
                    and (child / _BODY_FILE).is_file()
                ):
                    names.add(child.name)
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

    def use_when(self, name: str) -> str:
        """The ``Use when:`` line, which says when to load the skill, not how."""
        try:
            lines = self.load(name).splitlines()
        except FileNotFoundError:
            return ""
        prefix = USE_WHEN_PREFIX.lower()
        for line in lines[1:8]:
            stripped = line.strip()
            if stripped.lower().startswith(prefix):
                return stripped.split(":", 1)[1].strip()
        return ""

    def in_index(self, name: str) -> bool:
        """Public skills are indexed unless an early line is ``Index: no``."""
        try:
            lines = self.load(name).splitlines()
        except FileNotFoundError:
            return False
        for line in lines[1:8]:
            if line.strip().lower() == INDEX_OPT_OUT:
                return False
        return True

    def index_block(self) -> str:
        """One line per indexed skill. Bodies stay behind ``read_skill``."""
        rows = []
        for name in self.list_public():
            if not self.in_index(name):
                continue
            label = self.use_when(name) or self.blurb(name)
            if label:
                rows.append(f"- `{name}`: {label}")
        if not rows:
            return ""
        return (
            SKILL_INDEX_HEADING + "\n\n"
            "Call `read_skill` with the name before acting on a matching task. "
            "The line says when, not how. A user who says to just do it opts "
            "out of `design` only.\n\n"
            + "\n".join(rows)
            + "\n"
        )

    def read(self, name: str, part: str = "") -> str:
        """Skill body, or one sibling part. Does not run the skill.

        A skill marked ``Index: no`` is refused here. Slash dispatch loads it
        with ``load`` instead, so only the user can start it.
        """
        part = (part or "").strip()
        opted_out = (
            self.path(name) is not None
            and self.is_public(name)
            and not self.in_index(name)
        )
        if opted_out:
            return msg_skill_user_starts(name)
        if part:
            if not _valid_part(part):
                raise FileNotFoundError(msg_skill_missing(name, ", ".join(self.list_public())))
            path = self.part_path(name, part)
            if path is None:
                available = ", ".join(self.list_parts(name)) or "(none)"
                raise FileNotFoundError(
                    f"No part '{part}' for skill '{name}'. Parts: {available}"
                )
            return path.read_text()
        return self.load(name, public_only=True)

    def save_user(self, name: str, content: str) -> Path:
        """Write a confirmed skill into the user skills directory."""
        err = validate_skill_name(name)
        if err:
            raise ValueError(err)
        self.user.mkdir(parents=True, exist_ok=True)
        path = self.user / f"{name}.md"
        path.write_text(content if content.endswith("\n") else content + "\n")
        return path

    def system_prompt(self, cfg: Config, workspace_root: "Path | None" = None,
                      clock_now=None, memory_block: "str | None" = None) -> str:
        base = self.load("system")
        names = tools.tool_names(cfg=cfg)
        tools_block = (
            "\n\n" + TOOLS_HEADING + "\n\n"
            + ", ".join(f"`{n}`" for n in names)
            + ".\n"
        )
        # Inject the live tool list after the opening paragraph.
        if HOW_TO_WORK_MARKER in base:
            base = base.replace(HOW_TO_WORK_MARKER, tools_block + HOW_TO_WORK_MARKER, 1)
        else:
            base += tools_block
        base += "\n\n" + steer.clock_block(now=clock_now)
        steering = steer.steering_block(workspace_root)
        if steering:
            base += "\n\n" + steering
        index = self.index_block()
        if index:
            base += "\n\n" + index
        from ...capabilities.exec import DockerBackend, active_backend
        backend = active_backend()
        if isinstance(backend, DockerBackend) and backend.network == "bridge":
            base += "\n\n" + steer.sandbox_port_hint()
        from ...spirit import spirit_block
        spirit = spirit_block(cfg, workspace=workspace_root)
        if spirit:
            base += "\n\n" + spirit
        ctx = memory_block if memory_block is not None else memory.context_block(cfg)
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


def skill_prompt(name: str, task: str = "", *, public_only: bool = False) -> str:
    """Skill body plus an optional ``Task:`` block.

    Shared by ``/skill``, ``/memory audit``, and ``lmloop skill``.
    """
    body = load_skill(name, public_only=public_only)
    task = (task or "").strip()
    if not task:
        return body
    return body + "\n\nTask: " + task


def skill_blurb(name: str) -> str:
    return _library().blurb(name)


def skill_index_block() -> str:
    return _library().index_block()


def read_skill(name: str, part: str = "") -> str:
    """Return a public skill body or sibling part. Raises FileNotFoundError."""
    return _library().read(name, part)


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


def system_prompt(cfg: Config, workspace_root: "Path | None" = None,
                  clock_now=None, memory_block: "str | None" = None) -> str:
    return _library().system_prompt(
        cfg, workspace_root, clock_now=clock_now, memory_block=memory_block,
    )

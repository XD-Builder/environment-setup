"""Single command table for CLI stems, slash stems, and reserved skill names.

Handlers stay in cli.py / repl.py; this module owns names and metadata only.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class CommandMeta:
    """Stem without leading slash (e.g. 'help', 'continue')."""
    name: str
    desc: str
    accepts_arg: bool = False
    arg_hint: str = ""
    exits: bool = False
    slash: bool = True
    cli: bool = False
    # Block creating a user skill with this name.
    reserve_skill: bool = True


# Core commands. Skill shortcuts (/investigate, …) are added dynamically in repl.
COMMANDS: tuple = (
    CommandMeta("help", "show available commands"),
    CommandMeta("stats", "show token usage and session activity"),
    CommandMeta("transcript", "view rendered session in less (q to quit)"),
    CommandMeta("skills", "list skills, or: new <name> [brief] to author one",
                accepts_arg=True, arg_hint="[new <name> brief]", cli=True),
    CommandMeta("skill", "run any skill prompt by name",
                accepts_arg=True, arg_hint="<name> [task]", cli=True),
    CommandMeta("history", "list recent session logs",
                accepts_arg=True, arg_hint="[n]", cli=True),
    CommandMeta("checkpoints", "list saved checkpoints",
                accepts_arg=True, arg_hint="[n]"),
    CommandMeta("restore", "reload a prior session (shows last result) or checkpoint",
                accepts_arg=True, arg_hint="[session|checkpoint] <query> [fresh]"),
    CommandMeta("decisions", "show active project decisions", cli=True),
    CommandMeta("context", "show memory injected into the system prompt"),
    CommandMeta("continue", "resume after max_rounds or an interruption",
                accepts_arg=True, arg_hint="[message]"),
    CommandMeta("undo", "drop the last user turn from the in-memory thread"),
    CommandMeta("compact", "summarize thread in a side session; optional replace",
                accepts_arg=True, arg_hint="[focus]", reserve_skill=False),
    CommandMeta("new", "reset conversation (memory context re-injected)"),
    CommandMeta("model", "switch model, or list models with no argument",
                accepts_arg=True, arg_hint="<name>"),
    CommandMeta("models", "list models on the server", slash=False, cli=True),
    CommandMeta("memory", "show project learnings",
                accepts_arg=True, arg_hint="[query]", cli=True),
    CommandMeta("save", "checkpoint session for later restore",
                accepts_arg=True, arg_hint="[title]"),
    CommandMeta("retro", "extract learnings from this session (or last n)",
                accepts_arg=True, arg_hint="[n]", cli=True, reserve_skill=False),
    CommandMeta("config", "get/set lmloop settings",
                accepts_arg=True, arg_hint="[get|set] …", slash=False, cli=True),
    CommandMeta("completion", "shell completion script",
                accepts_arg=True, arg_hint="zsh", slash=False, cli=True),
    CommandMeta("quit", "exit", exits=True),
    CommandMeta("exit", "exit", exits=True),
    CommandMeta("q", "exit", exits=True),
)

# Extra stems that are not top-level commands but must not become skill names.
_META_RESERVED = frozenset({"system", "names"})


def reserved_skill_names() -> "frozenset[str]":
    names = {c.name for c in COMMANDS if c.reserve_skill}
    return frozenset(names | _META_RESERVED)


def slash_command_metas() -> "list[CommandMeta]":
    return [c for c in COMMANDS if c.slash]


def cli_subcommand_metas() -> "list[CommandMeta]":
    return [c for c in COMMANDS if c.cli]


def cli_subcommand_names() -> "frozenset[str]":
    return frozenset(c.name for c in COMMANDS if c.cli)


RESERVED_SKILL_NAMES = reserved_skill_names()

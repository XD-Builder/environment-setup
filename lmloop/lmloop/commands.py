"""Single command table for CLI stems, slash stems, and reserved skill names.

Handlers stay in cli.py / repl.py; this module owns names and metadata only.
Memory and flow argument shapes live here so both surfaces parse the same way.
"""

from dataclasses import dataclass
from typing import Literal, cast


@dataclass(frozen=True)
class CommandMeta:
    """Stem without leading slash (e.g. 'help', 'continue')."""
    name: str
    desc: str
    accepts_arg: bool = False
    arg_hint: str = ""
    arg_choices: tuple = ()
    exits: bool = False
    slash: bool = True
    cli: bool = False
    # ``advanced`` rows appear in ``/help all`` only; omitted from ``/`` completion.
    help_tier: str = "core"
    # Block creating a user skill with this name.
    reserve_skill: bool = True


# First-token verbs for ``/memory`` and ``lmloop memory`` (not search queries).
MEMORY_ARG_CHOICES = (
    "list", "decisions", "dump", "mine", "kg", "graph", "reconcile",
    "index", "reindex", "canvas", "audit",
)
MEMORY_ARG_HINT = (
    "[list | decisions | dump | query | mine [n] | kg | index | reindex | "
    "canvas | audit | reconcile]"
)
# Subverbs offered in ``/`` completion (``graph`` kept as runtime alias only).
MEMORY_ARG_COMPLETION = (
    "list", "decisions", "dump", "mine", "kg", "reconcile", "index", "reindex",
    "canvas", "audit",
)

# Skill names that get ``/name`` but not default ``/`` completion.
ADVANCED_SKILL_SLASH = frozenset({"learn", "retro"})

MSG_DEPRECATE_DECISIONS = "[deprecated: use memory decisions]"
MSG_DEPRECATE_MEMORY_GRAPH = (
    "[deprecated: use /memory kg — workflow graphs are /graph <name>]"
)
MSG_DEPRECATE_RETRO = "[deprecated: use /memory mine]"

MemoryVerb = Literal[
    "list", "decisions", "dump", "mine", "kg", "index", "reindex",
    "canvas", "audit", "reconcile", "search",
]
# Verbs ``memory.render_memory_view`` can print without a model.
MEMORY_VIEW_VERBS = frozenset({
    "list", "decisions", "dump", "kg", "index", "reindex", "canvas", "search",
})


def parse_positive_count(token: str) -> int | None:
    """A positive integer token, or None when ``token`` is not one."""
    if token.isdigit() and int(token) > 0:
        return int(token)
    return None


@dataclass(frozen=True)
class MemoryRequest:
    """Parsed ``memory`` / ``/memory`` invocation. ``error`` means do not run."""

    verb: MemoryVerb
    rest: tuple[str, ...] = ()
    query: str = ""
    mine_count: int | None = None
    error: str = ""
    detail: str = ""
    hint: str = ""


def parse_memory_words(
    words: list[str],
    *,
    mine_default: int | None,
    mine_usage: str,
    mine_detail: str = "",
) -> MemoryRequest:
    """Parse memory words.

    ``mine_default`` is the count when ``mine`` has no argument (CLI uses 3).
    ``None`` means mine this session (REPL). A non-positive or extra token
    sets ``error`` and does not invent a count.
    """
    if not words or words[0] == "list":
        return MemoryRequest(verb="list")
    verb = words[0]
    rest = tuple(words[1:])
    if verb == "graph":
        return MemoryRequest(verb="kg", hint=MSG_DEPRECATE_MEMORY_GRAPH)
    if verb == "mine":
        if not rest:
            return MemoryRequest(verb="mine", mine_count=mine_default)
        if len(rest) == 1:
            count = parse_positive_count(rest[0])
            if count is not None:
                return MemoryRequest(verb="mine", mine_count=count, rest=rest)
        return MemoryRequest(verb="mine", error=mine_usage, detail=mine_detail)
    if verb in MEMORY_ARG_CHOICES:
        return MemoryRequest(verb=cast(MemoryVerb, verb), rest=rest, query=" ".join(rest))
    return MemoryRequest(verb="search", rest=tuple(words), query=" ".join(words))


def flow_usage(invocation: str) -> str:
    """Usage line for ``lmloop flow`` and ``/flow``."""
    return f"usage: {invocation} [--json]"


@dataclass(frozen=True)
class FlowRequest:
    """Parsed ``flow`` / ``/flow`` invocation."""

    as_json: bool = False
    error: str = ""


def parse_flow_words(words: list[str], *, invocation: str) -> FlowRequest:
    """Accept no args or a single ``--json``. Anything else is ``error``."""
    if not words:
        return FlowRequest(as_json=False)
    if words == ["--json"]:
        return FlowRequest(as_json=True)
    return FlowRequest(error=flow_usage(invocation))


# Window sizes when ``history`` / ``checkpoints`` are given no count.
HISTORY_DEFAULT_CLI = 15
HISTORY_DEFAULT_REPL = 10
CHECKPOINT_DEFAULT = 10


def count_usage(invocation: str) -> str:
    """Usage line for a command that takes an optional positive count."""
    return f"usage: {invocation} [n]"


@dataclass(frozen=True)
class CountRequest:
    """Parsed optional positive count. ``error`` means do not list."""

    count: int = 0
    error: str = ""


def parse_count_words(
    words: list[str],
    *,
    default: int,
    usage: str,
) -> CountRequest:
    """No tokens → ``default``. One positive integer → that count. Else ``error``."""
    tokens = [word for word in words if word]
    if not tokens:
        return CountRequest(count=default)
    if len(tokens) == 1:
        count = parse_positive_count(tokens[0])
        if count is not None:
            return CountRequest(count=count)
    return CountRequest(error=usage)


EvalMode = Literal[
    "text", "json", "design", "abstention", "gate", "drain", "inbox",
]
EVAL_JSON_FLAG = "--json"
EVAL_DESIGN_FLAGS = ("--design", "--design-doc")
EVAL_ABSTENTION_FLAG = "--abstention"
EVAL_GATE_FLAG = "--gate"
EVAL_DRAIN_FLAG = "--drain"
EVAL_INBOX_FLAG = "--inbox"
EVAL_GATE_NAMES = ("commit", "pr", "nightly")
EVAL_FLAGS = (
    EVAL_JSON_FLAG, *EVAL_DESIGN_FLAGS, EVAL_ABSTENTION_FLAG,
    EVAL_GATE_FLAG, EVAL_DRAIN_FLAG, EVAL_INBOX_FLAG,
)


def eval_usage(invocation: str = "lmloop eval") -> str:
    """Usage line for ``lmloop eval``. ``--design-doc`` is an alias of ``--design``."""
    return (
        f"usage: {invocation} [--json | --design | --abstention | "
        "--gate commit|pr|nightly | --drain | --inbox]"
    )


@dataclass(frozen=True)
class EvalRequest:
    """Parsed ``lmloop eval`` invocation.

    ``--json`` may accompany ``--abstention``, ``--gate``, ``--drain``, or
    ``--inbox``. ``--design`` is exclusive. ``gate`` is set only for ``--gate``.
    """

    mode: EvalMode = "text"
    as_json: bool = False
    gate: str = ""
    error: str = ""


def parse_eval_words(
    words: list[str],
    *,
    invocation: str = "lmloop eval",
) -> EvalRequest:
    """One eval mode. ``--json`` may combine with abstention, gate, drain, or inbox."""
    seen: list[str] = []
    gate = ""
    index = 0
    while index < len(words):
        word = words[index]
        if word == EVAL_JSON_FLAG:
            kind = "json"
        elif word in EVAL_DESIGN_FLAGS:
            kind = "design"
        elif word == EVAL_ABSTENTION_FLAG:
            kind = "abstention"
        elif word == EVAL_DRAIN_FLAG:
            kind = "drain"
        elif word == EVAL_INBOX_FLAG:
            kind = "inbox"
        elif word == EVAL_GATE_FLAG:
            kind = "gate"
            if index + 1 >= len(words) or words[index + 1] not in EVAL_GATE_NAMES:
                return EvalRequest(error=eval_usage(invocation))
            index += 1
            gate = words[index]
        else:
            return EvalRequest(error=eval_usage(invocation))
        if kind in seen:
            return EvalRequest(error=eval_usage(invocation))
        seen.append(kind)
        index += 1
    kinds = set(seen)
    if "design" in kinds and kinds != {"design"}:
        return EvalRequest(error=eval_usage(invocation))
    primary = kinds - {"json"}
    if len(primary) > 1:
        return EvalRequest(error=eval_usage(invocation))
    as_json = "json" in kinds
    if "abstention" in kinds:
        return EvalRequest(mode="abstention", as_json=as_json)
    if "gate" in kinds:
        return EvalRequest(mode="gate", as_json=as_json, gate=gate)
    if "drain" in kinds:
        return EvalRequest(mode="drain", as_json=as_json)
    if "inbox" in kinds:
        return EvalRequest(mode="inbox", as_json=as_json)
    if kinds == {"json"}:
        return EvalRequest(mode="json")
    if kinds == {"design"}:
        return EvalRequest(mode="design")
    return EvalRequest(mode="text")


GRAPH_PROPOSE_VERB = "propose"
GraphAction = Literal["resume", "run", "propose"]

MSG_RECONCILE_INCOMPLETE = "memory reconcile did not finish"


@dataclass(frozen=True)
class GraphRequest:
    """Parsed ``graph`` / ``/graph`` invocation.

    Empty words are ``resume``. The CLI resumes the latest open run; the REPL
    prints usage and leaves resume to ``/continue``.
    """

    action: GraphAction
    name: str = ""
    error: str = ""


def parse_graph_words(
    words: list[str],
    *,
    run_usage: str,
    propose_usage: str,
) -> GraphRequest:
    """``propose <name>``, a single graph name, or no words (resume)."""
    if not words:
        return GraphRequest(action="resume")
    if words[0] == GRAPH_PROPOSE_VERB:
        if len(words) != 2 or not words[1].strip():
            return GraphRequest(action="propose", error=propose_usage)
        return GraphRequest(action="propose", name=words[1])
    if len(words) != 1 or not words[0].strip():
        return GraphRequest(action="run", error=run_usage)
    return GraphRequest(action="run", name=words[0])


# Core commands. Skill shortcuts (/investigate, …) are added dynamically in repl.
COMMANDS: tuple = (
    CommandMeta("help", "show core commands (/help all for restore, compact, …)",
                accepts_arg=True, arg_hint="[all]"),
    CommandMeta("stats", "show token usage, session activity, and memory HUD"),
    CommandMeta("transcript", "view rendered session in less (q to quit)"),
    CommandMeta("copy", "copy last answer to the clipboard (plain text, no live bar)",
                accepts_arg=True, arg_hint="[transcript]"),
    CommandMeta("skills", "list skills, or: new <name> [brief] to author one",
                accepts_arg=True, arg_hint="[new <name> brief]", cli=True),
    CommandMeta("skill", "run any skill prompt by name",
                accepts_arg=True, arg_hint="<name> [task]", cli=True),
    CommandMeta("history", "list recent session logs",
                accepts_arg=True, arg_hint="[n]", cli=True),
    CommandMeta("checkpoints", "list saved checkpoints",
                accepts_arg=True, arg_hint="[n]", help_tier="advanced"),
    CommandMeta("restore", "reload a prior session (shows last result) or checkpoint",
                accepts_arg=True, arg_hint="[session|checkpoint] <query> [fresh]",
                help_tier="advanced"),
    CommandMeta("decisions", "deprecated — use /memory decisions",
                cli=True, help_tier="advanced"),
    CommandMeta("context", "show this conversation's files and memory"),
    CommandMeta("continue", "resume after max_rounds, an interruption, or a paused until/graph run",
                accepts_arg=True, arg_hint="[message]"),
    CommandMeta("undo", "drop the last user turn from the in-memory thread",
                help_tier="advanced"),
    CommandMeta("compact", "summarize thread in a side session; optional replace",
                accepts_arg=True, arg_hint="[focus]", reserve_skill=False,
                help_tier="advanced"),
    CommandMeta("new", "reset conversation (memory context re-injected)"),
    CommandMeta("model", "switch model, or list models with no argument",
                accepts_arg=True, arg_hint="<name>"),
    CommandMeta("models", "list models on the server", slash=False, cli=True),
    CommandMeta("memory", "project memory dashboard; subverbs: dump | mine | kg | …",
                accepts_arg=True,
                arg_hint=MEMORY_ARG_HINT,
                arg_choices=MEMORY_ARG_CHOICES, cli=True),
    CommandMeta("until", "work toward a goal until a check or evaluator passes",
                accepts_arg=True, arg_hint="[--check cmd] [--keep cmd] <goal>", cli=True),
    CommandMeta("graph", "run an authored workflow graph",
                accepts_arg=True, arg_hint="<name> | propose <name>", cli=True),
    CommandMeta("flow", "workflow stats and rule-based suggestions from run logs",
                accepts_arg=True, arg_hint="[--json]", cli=True),
    CommandMeta("save", "checkpoint session for later restore",
                accepts_arg=True, arg_hint="[title]"),
    # CLI-only alias so `lmloop retro` stays in the registry (not a /help peer).
    CommandMeta("retro", "alias for memory mine",
                accepts_arg=True, arg_hint="[n]", slash=False, cli=True,
                reserve_skill=False),
    CommandMeta("config", "get/set lmloop settings",
                accepts_arg=True, arg_hint="[get|set] …", slash=False, cli=True),
    CommandMeta("completion", "shell completion script",
                accepts_arg=True, arg_hint="zsh", slash=False, cli=True),
    CommandMeta(
        "eval",
        "usage gaps, abstention pairs, assertion gates, and the trace inbox",
        accepts_arg=True,
        arg_hint="[--json | --design | --abstention | --gate commit|pr|nightly | --drain | --inbox]",
        slash=False, cli=True, reserve_skill=False),
    CommandMeta("sandbox", "Docker sandbox status, build, reset (opt-in --docker)",
                accepts_arg=True,
                arg_hint="[status | build | shell | reset [--deps] | rm]",
                arg_choices=("build", "status", "shell", "reset", "rm"),
                cli=True),
    CommandMeta("company", "opt-in multi-agent company (needs --docker and a remote model)",
                accepts_arg=True, arg_hint="run --goal TEXT [--campaign ID] | manifest",
                arg_choices=("run", "manifest"), cli=True),
    CommandMeta("worker", "headless company worker (stdin packet, stdout envelope)",
                accepts_arg=True, arg_hint="run", slash=False, cli=True),
    CommandMeta("campaign", "multi-day campaign: start, resume, status, board, extend",
                accepts_arg=True,
                arg_hint="start --goal TEXT | resume | status | board | extend",
                arg_choices=("start", "resume", "status", "board", "extend"),
                cli=True),
    CommandMeta("spirit", "project spirit: log, review, or distill thoughts into self",
                accepts_arg=True, arg_hint="log | review | distill [--patch file]",
                arg_choices=("log", "review", "distill"), cli=True),
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

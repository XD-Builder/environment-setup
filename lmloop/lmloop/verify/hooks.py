"""PreToolUse / PostToolUse hooks: subprocesses that can veto or annotate a tool.

Hooks load from ``~/.lmloop/hooks.json`` (every project) and
``~/.lmloop/projects/<slug>/hooks.json`` (this project). Neither lives in the
repository, so a clone cannot make lmloop run its commands::

    {"PreToolUse":  [{"matcher": "run_shell", "command": "~/bin/pre-shell.sh"}],
     "PostToolUse": [{"matcher": "mcp__github__*", "command": "notify-send done"}]}

A hook reads ``{"event", "tool", "args", "cwd", "project_dir"}`` (plus
``result`` after the call) as JSON on stdin. Stdout may be
``{"continue": false, "message": "…"}``; empty stdout continues. A non-zero
exit or a timeout blocks a PreToolUse call and annotates a PostToolUse result.
``matcher`` is a glob over the tool name; a lazy ``mcp_call`` also matches as
``mcp__<server>__<tool>``.
"""

import fnmatch
import functools
import json
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path

from ..capabilities.tools import ShellCommand, ToolDef, ToolResult
from ..core import config, usage

HOOKS_FILE = "hooks.json"
HOOK_TIMEOUT_S = 10
EVENT_PRE = "PreToolUse"
EVENT_POST = "PostToolUse"
EVENTS = (EVENT_PRE, EVENT_POST)
_RESULT_CLIP = 4000
_MESSAGE_CLIP = 500


@dataclass(frozen=True)
class Hook:
    event: str
    matcher: str
    command: str
    source: str

    def matches(self, names: "tuple[str, ...]") -> bool:
        return any(fnmatch.fnmatchcase(n, self.matcher) for n in names)


def hook_files(workspace_root: Path) -> "list[Path]":
    slug = config.cached_project_slug(Path(workspace_root).resolve())
    return [
        config.STATE_ROOT / HOOKS_FILE,
        config.STATE_ROOT / "projects" / slug / HOOKS_FILE,
    ]


def parse_hooks(raw, source: str) -> "tuple[list[Hook], list[str]]":
    """Hooks from one file's JSON plus error lines for rows that were skipped."""
    if not isinstance(raw, dict):
        return [], [f"{source}: expected a JSON object"]
    hooks, errors = [], []
    for event in EVENTS:
        for row in raw.get(event) or []:
            command = row.get("command") if isinstance(row, dict) else None
            if not isinstance(command, str) or not command.strip():
                errors.append(f"{source}: {event} entry needs a command")
                continue
            if command.startswith("~"):
                command = str(Path(command).expanduser())
            hooks.append(Hook(event, str(row.get("matcher") or "*"), command, source))
    return hooks, errors


def _match_names(name: str, args: dict) -> "tuple[str, ...]":
    from ..mcp.tooldefs import MCP_CALL, eager_name

    if name == MCP_CALL and args.get("server") and args.get("tool"):
        return name, eager_name(str(args["server"]), str(args["tool"]))
    return (name,)


@dataclass
class HookRunner:
    hooks: list
    workspace: Path
    errors: list

    @classmethod
    def load(cls, workspace_root: "Path | None" = None) -> "HookRunner":
        root = Path(workspace_root or Path.cwd()).resolve()
        hooks, errors = [], []
        for path in hook_files(root):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                continue
            except (OSError, json.JSONDecodeError) as exc:
                errors.append(f"{path}: {exc}")
                continue
            found, bad = parse_hooks(raw, str(path))
            hooks.extend(found)
            errors.extend(bad)
        return cls(hooks, root, errors)

    def describe_lines(self) -> "list[str]":
        if not self.hooks and not self.errors:
            files = ", ".join(str(p) for p in hook_files(self.workspace))
            return [f"Hooks: none (add to {files})"]
        lines = ["Hooks:"]
        lines.extend(f"  {h.event} {h.matcher} -> {h.command}" for h in self.hooks)
        lines.extend(f"  error: {e}" for e in self.errors)
        return lines

    def _run(self, hook: Hook, payload: dict) -> "tuple[bool, str]":
        """(continue, message). A hook that cannot run says stop."""
        parsed = ShellCommand(hook.command)
        argv = ["/bin/sh", "-c", hook.command] if parsed.needs_shell() else parsed.argv
        try:
            proc = subprocess.run(
                argv, input=json.dumps(payload), capture_output=True, text=True,
                timeout=HOOK_TIMEOUT_S, cwd=str(self.workspace),
            )
        except subprocess.TimeoutExpired:
            return False, f"hook timed out after {HOOK_TIMEOUT_S}s: {hook.command}"
        except OSError as exc:
            return False, f"hook could not start ({hook.command}): {exc}"
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()[:_MESSAGE_CLIP]
            return False, f"hook exited {proc.returncode} ({hook.command})" + (
                f": {detail}" if detail else ""
            )
        try:
            reply = json.loads(proc.stdout) if proc.stdout.strip() else {}
        except json.JSONDecodeError:
            return True, ""
        if not isinstance(reply, dict):
            return True, ""
        message = str(reply.get("message") or "")[:_MESSAGE_CLIP]
        return reply.get("continue", True) is not False, message

    def _payload(self, event: str, name: str, args: dict) -> dict:
        return {"event": event, "tool": name, "args": args,
                "cwd": str(self.workspace), "project_dir": str(self.workspace)}

    def pre(self, name: str, args: dict) -> "str | None":
        """None to run the tool, else the DENIED text for the model."""
        names = _match_names(name, args)
        for hook in self.hooks:
            if hook.event != EVENT_PRE or not hook.matches(names):
                continue
            ok, message = self._run(hook, self._payload(EVENT_PRE, name, args))
            if not ok:
                usage.record("hook.blocked", tool=name)
                return f"DENIED: a PreToolUse hook blocked {name}: {message or 'no reason given'}"
        return None

    def post(self, name: str, args: dict, result: str) -> str:
        """Notes to append to the result (empty when hooks are silent)."""
        names = _match_names(name, args)
        notes = []
        for hook in self.hooks:
            if hook.event != EVENT_POST or not hook.matches(names):
                continue
            payload = self._payload(EVENT_POST, name, args)
            payload["result"] = result[:_RESULT_CLIP]
            ok, message = self._run(hook, payload)
            if message or not ok:
                notes.append(f"[hook] {message or 'PostToolUse hook failed'}")
        return "\n".join(notes)

    def wrap(self, tool: ToolDef) -> ToolDef:
        """Same row, impl guarded by matching hooks. No match: the row unchanged."""
        from ..mcp.tooldefs import MCP_CALL

        if tool.name != MCP_CALL and not any(h.matches((tool.name,)) for h in self.hooks):
            return tool
        impl = tool.impl

        @functools.wraps(impl)
        def guarded(**kwargs):
            blocked = self.pre(tool.name, kwargs)
            if blocked:
                return blocked
            result = impl(**kwargs)
            note = self.post(tool.name, kwargs, str(result))
            if not note:
                return result
            if isinstance(result, ToolResult):
                return replace(result, text=f"{result.text}\n{note}")
            return f"{result}\n{note}"

        return replace(tool, impl=guarded)

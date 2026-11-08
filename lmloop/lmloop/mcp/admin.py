"""``lmloop mcp …`` and ``/mcp …``: one parser and one report for both surfaces."""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..core.commands import MCP_VERBS
from .client import McpError
from .config import (
    SCOPE_LOCAL,
    SCOPES,
    TRANSPORT_HTTP,
    McpConfigError,
    McpPolicy,
    ServerSpec,
    add_server,
    approve_servers,
    entry_fingerprint,
    find_entry,
    remove_server,
    set_disabled,
    tool_is_read_only,
)
from .session import McpSessionManager, clip, session_manager

_VAR_REF_RE = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*(:-[^}]*)?\}$")
_REDACTED = "••••"
_VERB_ALIASES = {"rm": "remove", "ls": "list"}

USAGE = """usage: lmloop mcp <verb>
  list                               servers, scope, and state (default)
  get <name>                         one entry (secrets redacted)
  add [-s local|project|user] [-e K=V] [-H 'K: V'] <name> <url | -- command args…>
  add-json [-s scope] <name> '<json>'
  remove [-s scope] <name>
  approve [names… | --all]           trust project servers from .mcp.json
  enable|disable <name>
  tools [name] [--refresh]           list tools (connects)
  reconnect [name]                   drop sessions and cached tool lists
  policy                             show .lmloop/mcp.policy.json"""


@dataclass
class McpReport:
    code: int = 0
    out: list = field(default_factory=list)
    err: list = field(default_factory=list)

    def fail(self, message: str) -> "McpReport":
        self.code = 1
        self.err.append(message)
        return self


def _redact(entry: dict) -> dict:
    shown = dict(entry)
    for key in ("env", "headers"):
        if isinstance(shown.get(key), dict):
            shown[key] = {
                k: v if isinstance(v, str) and _VAR_REF_RE.match(v) else _REDACTED
                for k, v in shown[key].items()
            }
    return shown


def pending_approval_lines(manager: McpSessionManager) -> "list[str]":
    """One line per project server waiting for approval (empty when none)."""
    return [
        f"  {spec.name}: {spec.describe()}" for spec in manager.config.unapproved
    ]


def _take_scope(words: list, default: str) -> "tuple[str, list, str]":
    rest, scope = [], default
    i = 0
    while i < len(words):
        if words[i] in ("-s", "--scope") and i + 1 < len(words):
            scope = words[i + 1]
            i += 2
            continue
        rest.append(words[i])
        i += 1
    if scope not in SCOPES:
        return scope, rest, f"scope must be one of {', '.join(SCOPES)}"
    return scope, rest, ""


def _parse_add(words: list) -> "tuple[str, str, dict, str]":
    """Returns (scope, name, entry, error)."""
    scope, transport = SCOPE_LOCAL, ""
    env: dict = {}
    headers: dict = {}
    positional: list = []
    command: list = []
    i = 0
    while i < len(words):
        word = words[i]
        if word == "--":
            command = words[i + 1:]
            break
        if word in ("-s", "--scope", "-t", "--transport", "-e", "--env", "-H", "--header"):
            if i + 1 >= len(words):
                return scope, "", {}, f"{word} needs a value"
            value = words[i + 1]
            i += 2
            if word in ("-s", "--scope"):
                scope = value
            elif word in ("-t", "--transport"):
                transport = value
            elif word in ("-e", "--env"):
                key, sep, val = value.partition("=")
                if not sep or not key:
                    return scope, "", {}, f"--env wants KEY=VALUE, got {value!r}"
                env[key] = val
            else:
                key, sep, val = value.partition(":")
                if not sep or not key.strip():
                    return scope, "", {}, f"--header wants 'Name: value', got {value!r}"
                headers[key.strip()] = val.strip()
            continue
        positional.append(word)
        i += 1
    if scope not in SCOPES:
        return scope, "", {}, f"scope must be one of {', '.join(SCOPES)}"
    if not positional:
        return scope, "", {}, "add needs a server name"
    name, rest = positional[0], positional[1:] + command
    if not rest:
        return scope, name, {}, "add needs a URL or `-- command args…`"
    is_url = rest[0].startswith(("http://", "https://"))
    if transport == TRANSPORT_HTTP or (not transport and is_url and not command):
        if len(rest) != 1:
            return scope, name, {}, "an http server takes exactly one URL"
        entry: dict = {"type": TRANSPORT_HTTP, "url": rest[0]}
        if headers:
            entry["headers"] = headers
        return scope, name, entry, ""
    entry = {"command": rest[0]}
    if rest[1:]:
        entry["args"] = rest[1:]
    if env:
        entry["env"] = env
    return scope, name, entry, ""


def _list(manager: McpSessionManager, report: McpReport) -> None:
    cfg = manager.config
    rows = manager.status_rows()
    if not rows and not cfg.unapproved and not cfg.skipped and not cfg.disabled:
        report.out.append(
            "No MCP servers. Add one: lmloop mcp add <name> <url> "
            "or lmloop mcp add <name> -- <command> [args…]"
        )
        return
    for row in rows:
        report.out.append(f"{row.name} [{row.scope} · {row.transport}] {row.state}")
        report.out.append(f"  {row.detail}")
    if cfg.unapproved:
        report.out.append("Waiting for approval (lmloop mcp approve <name> | --all):")
        report.out.extend(pending_approval_lines(manager))
    for name in cfg.disabled:
        report.out.append(f"{name} [disabled] — lmloop mcp enable {name}")
    for name, reason in cfg.skipped:
        report.out.append(f"{name} [skipped] {reason}")


def _tools(manager: McpSessionManager, words: list, report: McpReport) -> None:
    refresh = "--refresh" in words
    names = [w for w in words if w != "--refresh"] or sorted(manager.config.servers)
    if not names:
        report.out.append("No MCP servers configured.")
        return
    for name in names:
        try:
            listed = manager.tools(name, refresh=refresh)
        except McpError as exc:
            report.fail(f"{name}: {exc}")
            continue
        report.out.append(f"{name} — {len(listed)} tools")
        for tool in listed:
            mark = "" if tool_is_read_only(tool) else " [asks]"
            report.out.append(
                f"  {tool.get('name')}{mark}: {clip(tool.get('description') or '')}"
            )


def run_mcp_words(words: list, workspace_root: "Path | None" = None,
                  manager: "McpSessionManager | None" = None) -> McpReport:
    """Run one ``mcp`` invocation. Never raises for user errors."""
    manager = manager or session_manager()
    manager.configure(workspace_root)
    paths = manager.paths
    report = McpReport()
    verb = _VERB_ALIASES.get(words[0], words[0]) if words else "list"
    rest = list(words[1:])
    if verb in ("help", "-h", "--help"):
        report.out.append(USAGE)
        return report
    if verb not in MCP_VERBS:
        return report.fail(f"unknown mcp verb {verb!r}\n{USAGE}")
    try:
        if verb == "list":
            _list(manager, report)
        elif verb == "get":
            if len(rest) != 1:
                return report.fail("usage: lmloop mcp get <name>")
            found = find_entry(paths, rest[0])
            if found is None:
                return report.fail(f"no MCP server named {rest[0]!r}")
            scope, entry = found
            report.out.append(f"{rest[0]} [{scope}] {paths.scope_file(scope)}")
            report.out.append(json.dumps(_redact(entry), indent=2, ensure_ascii=False))
        elif verb == "add":
            scope, name, entry, err = _parse_add(rest)
            if err:
                return report.fail(f"{err}\n{USAGE}")
            path = add_server(paths, scope, name, entry)
            report.out.append(f"Added {name} to {path}")
            if scope == "project":
                approve_servers(paths, [ServerSpec(name, scope, "", entry_fingerprint(entry))])
                report.out.append(f"Approved {name} for this machine (you added it).")
        elif verb == "add-json":
            scope, rest, err = _take_scope(rest, SCOPE_LOCAL)
            if err or len(rest) != 2:
                return report.fail(err or "usage: lmloop mcp add-json [-s scope] <name> '<json>'")
            try:
                entry = json.loads(rest[1])
            except json.JSONDecodeError as exc:
                return report.fail(f"invalid JSON: {exc}")
            path = add_server(paths, scope, rest[0], entry)
            report.out.append(f"Added {rest[0]} to {path}")
        elif verb == "remove":
            scope, rest, err = _scope_optional(rest)
            if err or len(rest) != 1:
                return report.fail(err or "usage: lmloop mcp remove [-s scope] <name>")
            manager.close(rest[0])
            removed = remove_server(paths, rest[0], scope)
            if not removed:
                return report.fail(f"no MCP server named {rest[0]!r}")
            report.out.extend(f"Removed {rest[0]} from {p}" for p in removed)
        elif verb == "approve":
            manager.invalidate()
            cfg = manager.configure(workspace_root)
            pending = {spec.name: spec for spec in cfg.unapproved}
            if not pending:
                report.out.append("Nothing waiting for approval.")
                return report
            if rest == ["--all"]:
                chosen = list(pending.values())
            elif rest:
                unknown = [n for n in rest if n not in pending]
                if unknown:
                    return report.fail("not waiting for approval: " + ", ".join(unknown))
                chosen = [pending[n] for n in rest]
            else:
                report.out.append("Waiting for approval (pass names or --all):")
                report.out.extend(pending_approval_lines(manager))
                return report
            names = approve_servers(paths, chosen)
            report.out.append("Approved: " + ", ".join(names))
        elif verb in ("enable", "disable"):
            if len(rest) != 1:
                return report.fail(f"usage: lmloop mcp {verb} <name>")
            if verb == "disable":
                manager.close(rest[0])
            set_disabled(paths, rest[0], verb == "disable")
            report.out.append(f"{verb}d {rest[0]}".capitalize())
        elif verb == "tools":
            _tools(manager, rest, report)
        elif verb == "reconnect":
            names = manager.reconnect(rest[0] if rest else "")
            report.out.append("Reconnect on next use: " + (", ".join(names) or "(none)"))
        elif verb == "policy":
            report.out.extend(McpPolicy.load(paths.workspace).describe_lines())
    except McpConfigError as exc:
        return report.fail(str(exc))
    finally:
        manager.invalidate()
        manager.configure(workspace_root)
    return report


def _scope_optional(words: list) -> "tuple[str, list, str]":
    scope, rest, err = _take_scope(words, SCOPE_LOCAL)
    explicit = any(w in ("-s", "--scope") for w in words)
    return (scope if explicit else ""), rest, err


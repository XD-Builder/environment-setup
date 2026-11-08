"""MCP server scopes, project-server approval, and the per-tool call policy.

Three scope files share the ``{"mcpServers": {...}}`` shape (Claude Code /
Cursor compatible). Precedence is local > project > user:

- project: ``<workspace>/.mcp.json`` (checked in; each entry needs approval)
- user:    ``~/.lmloop/mcp.json``
- local:   ``~/.lmloop/projects/<slug>/mcp.local.json`` (``lmloop mcp add`` default)

User and local files may list ``"disabledMcpServers": [...]``. Approvals live
in ``~/.lmloop/projects/<slug>/mcp.approved.json`` as name -> entry
fingerprint, so editing a checked-in entry asks again.

The policy file ``<workspace>/.lmloop/mcp.policy.json`` decides allow / ask /
deny per server and tool::

    {"default": "ask",
     "rules": [{"server": "github", "tools": ["create_*"], "action": "deny"},
               {"server": "postgres", "action": "allow"}]}

The first matching rule wins. With no match a read-only tool is allowed and
anything else gets ``default``. An unreadable policy never loosens gates.
"""

import fnmatch
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..core import config

SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
RESERVED_NAMES = frozenset({"workspace"})
SCOPE_LOCAL = "local"
SCOPE_PROJECT = "project"
SCOPE_USER = "user"
SCOPES = (SCOPE_LOCAL, SCOPE_PROJECT, SCOPE_USER)
TRANSPORT_STDIO = "stdio"
TRANSPORT_HTTP = "http"
_HTTP_TYPES = frozenset({"http", "streamable-http", "streamablehttp"})
_UNSUPPORTED_TYPES = {
    "sse": "legacy SSE transport is not supported; use the server's streamable HTTP url",
    "ws": "WebSocket transport is not supported",
    "sdk": "in-process sdk servers are not supported",
}
PROJECT_FILE = ".mcp.json"
USER_FILE = "mcp.json"
LOCAL_FILE = "mcp.local.json"
APPROVED_FILE = "mcp.approved.json"
SERVERS_KEY = "mcpServers"
DISABLED_KEY = "disabledMcpServers"

POLICY_FILE = Path(".lmloop") / "mcp.policy.json"
ACTION_ALLOW = "allow"
ACTION_ASK = "ask"
ACTION_DENY = "deny"
ACTIONS = (ACTION_ALLOW, ACTION_ASK, ACTION_DENY)
# First word of a tool name that marks a read (``list_issues``, ``get-user``).
READ_VERBS = frozenset({
    "get", "list", "search", "read", "query", "fetch", "find", "describe",
    "show", "view", "lookup", "browse", "count", "check", "inspect",
})
_NAME_SEPARATORS = "_-.:/"


class McpConfigError(ValueError):
    """A scope file or entry that cannot be used as written."""


@dataclass(frozen=True)
class McpPaths:
    """Every file that feeds one workspace's MCP config."""

    workspace: Path
    state_root: Path
    slug: str

    @classmethod
    def for_workspace(cls, workspace_root: "Path | None" = None) -> "McpPaths":
        root = Path(workspace_root or Path.cwd()).resolve()
        return cls(root, config.STATE_ROOT, config.cached_project_slug(root))

    @property
    def project(self) -> Path:
        return self.workspace / PROJECT_FILE

    @property
    def user(self) -> Path:
        return self.state_root / USER_FILE

    @property
    def local(self) -> Path:
        return self.state_root / "projects" / self.slug / LOCAL_FILE

    @property
    def approved(self) -> Path:
        return self.state_root / "projects" / self.slug / APPROVED_FILE

    @property
    def cache_dir(self) -> Path:
        return self.state_root / "projects" / self.slug / "cache" / "mcp"

    def scope_file(self, scope: str) -> Path:
        if scope == SCOPE_PROJECT:
            return self.project
        if scope == SCOPE_USER:
            return self.user
        if scope == SCOPE_LOCAL:
            return self.local
        raise McpConfigError(f"unknown scope '{scope}' (use {', '.join(SCOPES)})")

    def stamp(self) -> tuple:
        """Mtime and size of every input file (reload key)."""
        out = []
        for path in (self.user, self.project, self.local, self.approved):
            try:
                info = path.stat()
            except OSError:
                out.append((0, 0))
                continue
            out.append((info.st_mtime_ns, info.st_size))
        return tuple(out)


@dataclass(frozen=True)
class ServerSpec:
    """One resolved server entry. ``fingerprint`` hashes the unexpanded entry."""

    name: str
    scope: str
    transport: str
    fingerprint: str
    command: str = ""
    args: tuple = ()
    env: tuple = ()
    url: str = ""
    headers: tuple = ()

    def describe(self) -> str:
        """Command line or URL for status output. Header values never print."""
        if self.transport == TRANSPORT_HTTP:
            names = ", ".join(k for k, _ in self.headers)
            return self.url + (f"  [headers: {names}]" if names else "")
        return " ".join([self.command, *self.args])


@dataclass
class McpConfig:
    """Usable servers plus the ones that were skipped and why."""

    servers: dict = field(default_factory=dict)
    skipped: list = field(default_factory=list)
    unapproved: list = field(default_factory=list)
    disabled: list = field(default_factory=list)


def entry_fingerprint(entry: dict) -> str:
    canonical = json.dumps(entry, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def expand_vars(text: str, env: "dict | None" = None) -> "tuple[str, list[str]]":
    """Expand ``${VAR}`` and ``${VAR:-default}``. Returns (text, missing names).

    A missing variable without a default is reported so the caller can skip
    the server instead of sending an empty secret.
    """
    env = os.environ if env is None else env
    out: list[str] = []
    missing: list[str] = []
    i = 0
    while True:
        start = text.find("${", i)
        end = text.find("}", start + 2) if start >= 0 else -1
        if end < 0:
            out.append(text[i:])
            return "".join(out), missing
        out.append(text[i:start])
        name, sep, default = text[start + 2:end].partition(":-")
        value = env.get(name)
        if value is None or (sep and value == ""):
            if sep:
                value = default
            else:
                missing.append(name)
                value = ""
        out.append(value)
        i = end + 1


def _string_map(value) -> bool:
    return isinstance(value, dict) and all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    )


def validate_entry(name: str, entry) -> "str | None":
    """Shape check for one ``mcpServers`` entry. Returns an error or None."""
    if not SERVER_NAME_RE.match(name or ""):
        return "server name must be 1-64 chars of A-Z a-z 0-9 _ -"
    if name in RESERVED_NAMES:
        return f"server name '{name}' is reserved"
    if not isinstance(entry, dict):
        return "entry must be a JSON object"
    kind = str(entry.get("type") or "").strip().lower()
    if kind in _UNSUPPORTED_TYPES:
        return _UNSUPPORTED_TYPES[kind]
    if "url" in entry:
        if kind not in _HTTP_TYPES:
            return 'an entry with "url" needs "type": "http"'
        if not isinstance(entry["url"], str) or not entry["url"].strip():
            return '"url" must be a non-empty string'
        if not _string_map(entry.get("headers", {})):
            return '"headers" must map strings to strings'
        return None
    if kind and kind != TRANSPORT_STDIO:
        return f'unknown type "{kind}" (use "stdio" or "http")'
    if not isinstance(entry.get("command"), str) or not entry["command"].strip():
        return 'a stdio entry needs "command"'
    args = entry.get("args", [])
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        return '"args" must be a list of strings'
    if not _string_map(entry.get("env", {})):
        return '"env" must map strings to strings'
    return None


def resolve_entry(name: str, entry: dict, scope: str,
                  env: "dict | None" = None) -> "tuple[ServerSpec | None, str]":
    """Validate and expand one entry. Returns (spec, "") or (None, reason)."""
    err = validate_entry(name, entry)
    if err:
        return None, err
    missing: list[str] = []

    def expand(text: str) -> str:
        value, gone = expand_vars(text, env)
        missing.extend(gone)
        return value

    fingerprint = entry_fingerprint(entry)
    if "url" in entry:
        spec = ServerSpec(
            name=name, scope=scope, transport=TRANSPORT_HTTP, fingerprint=fingerprint,
            url=expand(entry["url"].strip()),
            headers=tuple(sorted(
                (k, expand(v)) for k, v in (entry.get("headers") or {}).items()
            )),
        )
    else:
        spec = ServerSpec(
            name=name, scope=scope, transport=TRANSPORT_STDIO, fingerprint=fingerprint,
            command=expand(entry["command"].strip()),
            args=tuple(expand(a) for a in entry.get("args") or []),
            env=tuple(sorted(
                (k, expand(v)) for k, v in (entry.get("env") or {}).items()
            )),
        )
    if missing:
        return None, "missing environment variable(s): " + ", ".join(sorted(set(missing)))
    return spec, ""


def read_scope_file(path: Path) -> dict:
    """Raw JSON object of one scope file; ``{}`` when the file is absent."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise McpConfigError(f"{path}: {exc}") from exc
    try:
        data = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError as exc:
        raise McpConfigError(f"{path}: {exc}") from exc
    if not isinstance(data, dict):
        raise McpConfigError(f"{path}: expected a JSON object")
    if not isinstance(data.get(SERVERS_KEY, {}), dict):
        raise McpConfigError(f'{path}: "{SERVERS_KEY}" must be an object')
    return data


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def read_approvals(paths: McpPaths) -> dict:
    try:
        data = json.loads(paths.approved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def load_mcp_config(paths: McpPaths, env: "dict | None" = None) -> McpConfig:
    """Merge user, project, and local scopes. Bad files are reported, not raised."""
    out = McpConfig()
    entries: dict = {}
    disabled: set = set()
    for scope in (SCOPE_USER, SCOPE_PROJECT, SCOPE_LOCAL):
        try:
            data = read_scope_file(paths.scope_file(scope))
        except McpConfigError as exc:
            out.skipped.append((scope, str(exc)))
            continue
        for name, entry in (data.get(SERVERS_KEY) or {}).items():
            entries[name] = (scope, entry)
        listed = data.get(DISABLED_KEY) if scope != SCOPE_PROJECT else None
        if isinstance(listed, list):
            disabled.update(str(n) for n in listed)
    approvals = read_approvals(paths)
    for name in sorted(entries):
        scope, entry = entries[name]
        if name in disabled:
            out.disabled.append(name)
            continue
        spec, reason = resolve_entry(name, entry, scope, env)
        if spec is None:
            out.skipped.append((name, reason))
        elif scope == SCOPE_PROJECT and approvals.get(name) != spec.fingerprint:
            out.unapproved.append(spec)
        else:
            out.servers[name] = spec
    return out


def approve_servers(paths: McpPaths, specs) -> "list[str]":
    """Record approval of these project-scope specs. Returns approved names."""
    approvals = read_approvals(paths)
    names = []
    for spec in specs:
        approvals[spec.name] = spec.fingerprint
        names.append(spec.name)
    if names:
        _write_json(paths.approved, approvals)
    return names


def add_server(paths: McpPaths, scope: str, name: str, entry: dict) -> Path:
    """Validate and write one entry into a scope file. Returns that file."""
    err = validate_entry(name, entry)
    if err:
        raise McpConfigError(err)
    path = paths.scope_file(scope)
    data = read_scope_file(path)
    data.setdefault(SERVERS_KEY, {})[name] = entry
    _write_json(path, data)
    return path


def remove_server(paths: McpPaths, name: str, scope: str = "") -> "list[Path]":
    """Drop ``name`` from one scope, or from every scope that has it."""
    removed = []
    for each in ([scope] if scope else list(SCOPES)):
        path = paths.scope_file(each)
        data = read_scope_file(path)
        servers = data.get(SERVERS_KEY) or {}
        if name in servers:
            del servers[name]
            _write_json(path, data)
            removed.append(path)
    return removed


def set_disabled(paths: McpPaths, name: str, disabled: bool) -> Path:
    """Toggle ``name`` in the local scope's ``disabledMcpServers`` list."""
    data = read_scope_file(paths.local)
    listed = [n for n in data.get(DISABLED_KEY) or [] if n != name]
    if disabled:
        listed.append(name)
    data[DISABLED_KEY] = sorted(listed)
    _write_json(paths.local, data)
    return paths.local


def find_entry(paths: McpPaths, name: str) -> "tuple[str, dict] | None":
    """Highest-precedence ``(scope, raw entry)`` for ``name``."""
    for scope in SCOPES:
        try:
            data = read_scope_file(paths.scope_file(scope))
        except McpConfigError:
            continue
        entry = (data.get(SERVERS_KEY) or {}).get(name)
        if entry is not None:
            return scope, entry
    return None


# --- call policy ------------------------------------------------------------

def tool_is_read_only(tool: dict) -> bool:
    """MCP annotations decide when present; otherwise the name's first word."""
    annotations = tool.get("annotations") or {}
    if annotations.get("destructiveHint") is True:
        return False
    if annotations.get("readOnlyHint") is not None:
        return annotations["readOnlyHint"] is True
    name = str(tool.get("name") or "").lower()
    for sep in _NAME_SEPARATORS:
        name = name.replace(sep, " ")
    words = name.split()
    return bool(words) and words[0] in READ_VERBS


@dataclass(frozen=True)
class PolicyRule:
    server: str = "*"
    tools: tuple = ("*",)
    action: str = ACTION_ASK

    def matches(self, server: str, tool: str) -> bool:
        return fnmatch.fnmatchcase(server, self.server) and any(
            fnmatch.fnmatchcase(tool, pattern) for pattern in self.tools
        )


@dataclass(frozen=True)
class McpPolicy:
    default: str = ACTION_ASK
    rules: tuple = ()
    source: str = ""
    error: str = ""

    @classmethod
    def load(cls, workspace_root: Path) -> "McpPolicy":
        path = Path(workspace_root) / POLICY_FILE
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls()
        except (OSError, json.JSONDecodeError) as exc:
            return cls(source=str(path), error=str(exc))
        return cls.from_dict(raw, source=str(path))

    @classmethod
    def from_dict(cls, raw, source: str = "") -> "McpPolicy":
        if not isinstance(raw, dict):
            return cls(source=source, error="policy must be a JSON object")
        default = str(raw.get("default") or ACTION_ASK).lower()
        if default not in ACTIONS:
            return cls(source=source, error=f"default must be one of {', '.join(ACTIONS)}")
        rules = []
        for row in raw.get("rules") or []:
            if not isinstance(row, dict):
                return cls(source=source, error="each rule must be an object")
            action = str(row.get("action") or "").lower()
            if action not in ACTIONS:
                return cls(source=source, error=f"rule action must be one of {', '.join(ACTIONS)}")
            tools = row.get("tools") or ["*"]
            if isinstance(tools, str):
                tools = [tools]
            rules.append(PolicyRule(
                str(row.get("server") or "*"), tuple(str(t) for t in tools), action,
            ))
        return cls(default=default, rules=tuple(rules), source=source)

    def decide(self, server: str, tool: str, read_only: bool) -> str:
        if self.error:
            return ACTION_ALLOW if read_only else ACTION_ASK
        for rule in self.rules:
            if rule.matches(server, tool):
                return rule.action
        return ACTION_ALLOW if read_only else self.default

    def describe_lines(self) -> "list[str]":
        if self.error:
            return [f"MCP policy {self.source}: unreadable ({self.error}) — mutating tools ask"]
        if not self.source:
            return ["MCP policy: none — read-only tools run, others ask"]
        lines = [f"MCP policy {self.source}: default {self.default}"]
        for rule in self.rules:
            lines.append(f"  {rule.server}/{','.join(rule.tools)} → {rule.action}")
        return lines

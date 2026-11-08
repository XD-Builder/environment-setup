# Guide: MCP servers in lmloop

lmloop can call tools on [Model Context Protocol](https://modelcontextprotocol.io)
servers: issue trackers, databases, browsers, docs search, and so on. This
guide walks through adding servers, sharing them with a repo, and controlling
what runs without asking. Reference tables are in the README
(“MCP servers, hooks, subagents, and project rules”).

## 1. Add a server

A local command (stdio):

```bash
lmloop mcp add files -- npx -y @modelcontextprotocol/server-filesystem .
```

A remote server (streamable HTTP), with the token read from your environment:

```bash
export DOCS_TOKEN=…
lmloop mcp add -H 'Authorization: Bearer ${DOCS_TOKEN}' docs https://mcp.example.com/mcp
```

Quote the header so your shell does not expand `${DOCS_TOKEN}`: lmloop stores
the reference and expands it each time it connects, so the secret never lands
in a file. If the variable is unset, the server is skipped and `lmloop mcp`
prints `missing environment variable(s): DOCS_TOKEN`.

`mcp add` writes to the **local** scope by default
(`~/.lmloop/projects/<slug>/mcp.local.json`). Use `-s user` for every project
or `-s project` to write the repo's `.mcp.json`.

Check it:

```bash
lmloop mcp                 # docs [local · http] not connected
lmloop mcp tools docs      # connects, lists tools, caches them for 24h
lmloop mcp get docs        # the entry, with env and header values redacted
```

Server stderr goes to `~/.lmloop/projects/<slug>/cache/mcp/logs/<name>.log`.

## 2. Use it

Nothing else to configure. The system prompt names each server and a few cached
tool names. The model searches with `mcp_find_tools("issue")` and calls with
`mcp_call(server, tool, arguments)`. A server process starts on first use and
stops when lmloop exits.

Prefer one row per tool? `lmloop config set mcp_tool_mode eager` adds each
cached tool as `mcp__<server>__<tool>` (up to 40 in total). Lazy mode keeps the
tool list short, which matters for small local models.

## 3. Share servers with a repo

Commit a `.mcp.json`:

```json
{"mcpServers": {
  "tracker": {"type": "http", "url": "https://mcp.example.com/mcp",
              "headers": {"Authorization": "Bearer ${TRACKER_TOKEN}"}},
  "db": {"command": "uvx", "args": ["some-db-mcp", "--read-only"]}
}}
```

A clone never starts these on its own. The REPL lists unapproved servers at
startup and asks once; `lmloop mcp approve --all` (or by name) does the same.
Approval is stored per entry, so if someone edits `db`'s command, it waits for
approval again.

## 4. Decide what runs without asking

| Tool kind | Default |
|---|---|
| Read-only (MCP `readOnlyHint`, or a name starting `get`, `list`, `search`, `read`, `find`, …) | runs |
| Anything else | asks y/N (`an MCP tool that may change external state`) |

Override per repo in `.lmloop/mcp.policy.json` (first matching rule wins):

```json
{"default": "ask",
 "rules": [
   {"server": "tracker", "tools": ["create_issue", "add_comment"], "action": "allow"},
   {"server": "*", "tools": ["delete_*", "drop_*"], "action": "deny"}
 ]}
```

`lmloop mcp policy` (or `/permissions`) prints what is in force. In `/until`
and `/graph` runs, MCP calls that need approval are in the irreversible tier:
they are refused during the step and offered once at the boundary. In
`/readonly` (plan mode) and in read-only `task` helpers, mutating MCP tools are
refused outright.

## 5. Results

MCP output is fenced as untrusted, like web pages: the model treats it as data,
not instructions. Images become vision input on VLMs. A result longer than
`max_tool_output` is saved under `~/.lmloop/projects/<slug>/mcp_results/` and
the model gets the start plus the path to page through with `read_file`.

## 6. Troubleshooting

| Symptom | Fix |
|---|---|
| `HTTP 401 … (set an Authorization header)` | Add `-H 'Authorization: Bearer ${VAR}'` and export the variable. OAuth sign-in is not supported. |
| Server shows `error: …` | Read the log named above; `lmloop mcp reconnect <name>` retries. |
| Tool list looks stale | `lmloop mcp tools <name> --refresh`. |
| Stop a server for now | `lmloop mcp disable <name>` (local only; `enable` undoes it). |
| `legacy SSE transport is not supported` | Use the server's streamable HTTP URL (often ends in `/mcp`). |

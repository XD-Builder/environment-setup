"""Tiny stdio MCP server for tests (stdlib only).

Tools: ``get_echo`` (read-only), ``create_note`` (mutating), ``big`` (long
text), ``fail`` (isError), ``roots`` (asks the client for roots/list).
``MCP_ECHO_TOOL_COUNT`` adds filler tools; ``MCP_ECHO_PAGE`` pages tools/list.
A non-JSON line on stdout at startup checks that clients skip log noise.
"""

import json
import os
import sys

TAG = os.environ.get("ECHO_TAG", "v1")
BASE_TOOLS = [
    {"name": "get_echo", "description": "Echo the text back",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}},
                     "required": ["text"]},
     "annotations": {"readOnlyHint": True}},
    {"name": "create_note", "description": "Create a note (changes state)",
     "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}}}},
    {"name": "big", "description": "Return a long text",
     "inputSchema": {"type": "object", "properties": {"size": {"type": "integer"}}},
     "annotations": {"readOnlyHint": True}},
    {"name": "fail", "description": "Always reports an error",
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"readOnlyHint": True}},
    {"name": "roots", "description": "List the client roots",
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"readOnlyHint": True}},
]
EXTRA = int(os.environ.get("MCP_ECHO_TOOL_COUNT", "0"))
TOOLS = BASE_TOOLS + [
    {"name": f"list_filler_{i}", "description": f"filler {i}",
     "inputSchema": {"type": "object", "properties": {}}}
    for i in range(EXTRA)
]
PAGE = int(os.environ.get("MCP_ECHO_PAGE", "0"))


def send(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def read() -> "dict | None":
    line = sys.stdin.readline()
    return json.loads(line) if line else None


def text(value: str) -> dict:
    return {"content": [{"type": "text", "text": value}]}


def call(name: str, args: dict) -> dict:
    if name == "get_echo":
        return text(f"{TAG}:{args.get('text', '')}")
    if name == "create_note":
        return text(f"created {args.get('title', '')}")
    if name == "big":
        return text("x" * int(args.get("size") or 50000))
    if name == "fail":
        return {"content": [{"type": "text", "text": "boom"}], "isError": True}
    if name == "roots":
        send({"jsonrpc": "2.0", "id": "r1", "method": "roots/list"})
        reply = read() or {}
        uris = [r["uri"] for r in (reply.get("result") or {}).get("roots", [])]
        return text("\n".join(uris))
    raise KeyError(name)


def main() -> None:
    print("echo server starting (not json)", flush=True)
    while True:
        msg = read()
        if msg is None:
            return
        if "id" not in msg:
            continue
        method, params = msg.get("method"), msg.get("params") or {}
        if method == "initialize":
            result = {"protocolVersion": params.get("protocolVersion"),
                      "capabilities": {"tools": {}}, "serverInfo": {"name": "echo"}}
        elif method == "tools/list":
            if PAGE:
                start = int(params.get("cursor") or 0)
                result = {"tools": TOOLS[start:start + PAGE]}
                if start + PAGE < len(TOOLS):
                    result["nextCursor"] = str(start + PAGE)
            else:
                result = {"tools": TOOLS}
        elif method == "tools/call":
            try:
                result = call(params.get("name"), params.get("arguments") or {})
            except KeyError:
                send({"jsonrpc": "2.0", "id": msg["id"],
                      "error": {"code": -32602, "message": "unknown tool"}})
                continue
        else:
            send({"jsonrpc": "2.0", "id": msg["id"],
                  "error": {"code": -32601, "message": "no such method"}})
            continue
        send({"jsonrpc": "2.0", "id": msg["id"], "result": result})


if __name__ == "__main__":
    main()

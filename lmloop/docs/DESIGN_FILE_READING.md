# Design: @path gifts and read_file windows

**Status:** shipped
**Date:** 2026-10-07
**Depends on:** `files_index.py`, `extract.py`, `tools.read_file`, `context.py`, `prompt.py`
**Companions:** [ARCHITECTURE.md](ARCHITECTURE.md) (tools table) · [DEVELOPMENT.md](../DEVELOPMENT.md) (module ownership)

Users attach files with `@path` on submit and the model reads them with `read_file`.
This doc is the contract for **how paths resolve**, **what gets inlined into the user
message**, and **how read results are windowed** — without a second registry or regex-heavy
parsers beyond the named `@` lexer constants in `files_index.py`.

---

## What shipped

| Piece | Where |
|-------|--------|
| `@path` lexer + expansion | [`files_index.py`](../lmloop/files_index.py) — `AT_REF_RE`, `collect_at_refs`, `append_referenced_files_block` |
| Completion (git index + fs) | `list_project_paths`, `complete_at_path` in `files_index.py`; wired from `prompt.py` |
| Path resolution (tools + @) | `resolve_user_path`, `normalize_typed_path` — same rules as `read_file` / writes |
| Referenced-files block | Appended on REPL submit; lists **token → resolved path**; PDF/Office/zip/audio inline via `extract.py` |
| `read_file` window + header | [`tools.py`](../lmloop/tools.py) — 400 lines default; `[continue with start_line=N]` |
| Workspace scope + @ bypass | `_check_workspace` + `extra_readable` from this turn's @ refs |
| Live thread manifest | [`context.py`](../lmloop/context.py) — `/context` Active files vs durable memory |
| Vision / audio | `extract.py` + `read_file` / @ attachments (images as parts when a VLM is loaded) |

---

## Mental model

1. **Gift (submit):** User `@` tokens that resolve to existing paths become readable **this
   turn** even outside the session workspace root. The model sees a short block naming
   resolved paths; binary types may include extracted text in the user message.
2. **Read (tool):** `read_file` returns numbered lines and an explicit next window when
   truncated. The ⚙ line and result header show the **resolved** path.
3. **Manifest (`/context`):** Scans in-memory messages for @ refs, attachments, image
   parts, and successful `read_file` results — not persisted under `~/.lmloop`.

---

## @path rules (summary)

| Token form | Resolution |
|------------|------------|
| `@relative/name` | Cwd at session start (workspace root for CLI) |
| `@~/…`, `@/abs`, `@./`, `@../` | Filesystem paths; duplicate slashes collapsed except `//` after scheme or UNC |
| `@"path with spaces"` / `@'…'` | Quoted; unquoted tokens extend last component against the filesystem |
| `@user@host.com` | Not a path (negative lookbehind on `@`) |

Missing paths warn on submit; existing paths always get a line in the referenced-files block.

---

## read_file header contract

- Default `max_lines`: 400 (`MAX_READ_LINES` in `tools.py`).
- Header includes total line count when known; when more lines remain, header names the
  exact continuation: `[continue with start_line=<next>]`.
- Non-text types delegate to `extract.extract_path`; errors start with `ERROR:`.
- Images return a `ToolResult` with vision parts instead of numbered text.

Steer and `skills/system.md` tell the model to use the header instead of guessing offsets.

---

## Non-goals (stay out of this module)

- Embedding or semantic search over file contents (see [DESIGN_MEMORY_RETRIEVAL.md](DESIGN_MEMORY_RETRIEVAL.md)).
- Watching the tree for auto-attach (user must `@` or the model must `read_file`).
- Reading arbitrary host paths without an @ gift or workspace scope.
- A second path alias syntax (only `@`, not `$file` or drag-drop IDs).

---

## Open questions

- Should `/context` list @ gifts from prior turns that are no longer in `extra_readable`?
- Cap on inlined extract size for huge PDFs (today: extract limits live in `extract.py`).

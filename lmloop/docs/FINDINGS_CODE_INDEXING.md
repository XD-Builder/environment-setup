# Findings: code understanding and workspace indexing

**Date:** 2026-11-06
**Scope:** How Cursor, Claude Code, Aider, Serena, and Augment make an agent understand a
workspace, and what lmloop should take from them.
**Companions:** [DESIGN_MEMORY_RETRIEVAL.md](DESIGN_MEMORY_RETRIEVAL.md) (memory index;
"embedding the codebase" rejected in Part 7) ·
[DESIGN_CLAUDE_CODE_PARITY.md](archived/DESIGN_CLAUDE_CODE_PARITY.md) (MCP client, shipped)

---

## Executive summary

1. **lmloop has no code index today.** Code understanding is `find_files` (cached
   `git ls-files` plus glob or substring match), `search_files` (ripgrep, `grep -rn`
   fallback), `read_file` (400 lines per call), and `list_dir`. The only persistent index
   is the FTS5 memory index, which covers learnings, decisions, sessions, and handoffs, not
   source code.
2. **The industry has split into two camps, and both use grep.** Claude Code relies on
   model-driven glob and grep with no index, plus an optional LSP tool. Cursor runs grep
   *and* a semantic embedding index, and recently added a local n-gram index so grep stays
   fast in huge monorepos. Nobody has dropped grep.
3. **Raw search speed is not lmloop's bottleneck.** ripgrep is fast on the repo sizes
   lmloop targets. The real costs are **round trips** (each one is a full local prefill) and
   **tokens per result** on small context windows. The improvements worth building make
   each call return denser, more structural answers, so the model needs fewer calls.
4. **Recommended order:** first tighten `search_files` output. Then add a ranked repo map
   and a per-file outline (stdlib `ast`, plus universal-ctags when it is on PATH). Then add
   symbol definition and reference lookup. An LSP or Serena backend comes after that, and
   embeddings last, only if evals show a vocabulary gap.

---

## 1. What lmloop does today

| Tool | Implementation | Notes |
|---|---|---|
| `find_files` | `files_index.list_project_paths` → `git ls-files -co --exclude-standard` (2 s TTL cache) or an `os.walk` capped at 20,000 paths | Up to 200 results; glob or substring |
| `search_files` | `rg --line-number --max-count 50 --max-columns 200` | `--max-count` is **per file**, so the total is bounded only by `max_tool_output` (12,000 chars) truncation |
| `read_file` | numbered lines, `MAX_READ_LINES = 400` | The model does not know where a symbol starts, so it reads in 400-line pages |
| `list_dir` | `iterdir` | |
| `recall_memory` | FTS5 `index.sqlite3` over memory JSONL | Not code |

Observed on this repo: every `search_files` hit carries the full absolute path
(`/agent/repos/environment-setup/lmloop/lmloop/run/until_runner.py:32:...`). A
`cfg_bool` search returned 47 lines in 5.6 KB. About a third of those bytes were the
absolute prefix that a workspace-relative path would drop, and grouping hits under one
heading per file would save more on top of that.

## 2. How the others do it

### Cursor: grep plus two indexes

- **Semantic index.** Files are split into syntactic chunks, embedded with a custom model,
  and synced with a Merkle tree of file and directory hashes, so only changed branches are
  re-chunked. Embeddings are cached by chunk content. Teammates' indexes are reused through
  a simhash match, which cut time to first query at p99 from 4.03 hours to 21 seconds
  ([Securely indexing large codebases](https://cursor.com/blog/secure-codebase-indexing)).
- **Measured value.** Adding semantic search alongside grep raised answer accuracy by
  12.5% on average (6.5–23.5% depending on the model). Code retention rose 0.3% overall and
  2.6% on codebases with 1,000+ files. Cursor says the *combination* of grep and semantic
  search wins. The embedding model is trained on agent traces: an LLM ranks what should
  have been retrieved earlier in each session
  ([Improving agent with semantic search](https://cursor.com/blog/semsearch)).
- **Fast regex.** In large monorepos, `rg` calls routinely took more than 15 seconds.
  Cursor built a local sparse n-gram inverted index (character-pair weights taken from code
  corpora) that narrows candidate files before matching. The index is anchored to a git
  commit, with user and agent edits layered on top, so the agent always sees its own writes
  ([Fast regex search](https://cursor.com/blog/fast-regex-search)).

### Claude Code: agentic search, no index

- Glob and grep, driven by the model. The team tried local vector databases and
  recursive model-based indexing; plain glob and grep won on "internal vibes", with no
  published benchmark. The reasons given were index drift, where an index lives (security),
  and exact-token quality. The stated cost is **latency and tokens**
  ([Pragmatic Engineer interview](https://newsletter.pragmaticengineer.com/p/building-claude-code-with-boris-cherny)).
- An optional **LSP tool** (since v2.0.74) offers `goToDefinition`, `findReferences`,
  `hover`, `documentSymbol`, `workspaceSymbol`, and call hierarchy. Users report that
  strong models rarely call it.
- The Explore subagent searches in its own context and returns a summary, which keeps
  search noise out of the main thread.

### Aider: ranked repo map

- tree-sitter extracts definitions and references, builds a file graph whose edges are
  symbol references, and runs **personalized PageRank**. The restart vector is biased
  toward files in the chat and identifiers the user mentioned. The top symbols that fit a
  token budget (`--map-tokens`, default 1k) go into the prompt
  ([Repository map](https://aider.chat/docs/repomap.html)).
- This is the closest fit for lmloop: it needs no model, no embeddings, and no daemon,
  and it is designed for a fixed token budget.

### Serena and Augment (MCP servers)

- **Serena** wraps language servers (or a JetBrains backend) as MCP tools:
  `get_symbols_overview`, `find_symbol`, `find_referencing_symbols`, and symbol-level edits
  such as `replace_symbol_body` and `insert_after_symbol`. It answers "where is this
  defined, and what references it?"
- **Augment Context Engine** offers semantic retrieval over a live local index and
  selected remote repos. It answers "which code is relevant to this behavior?"
- Both are reachable through lmloop's MCP client ([GUIDE_MCP.md](GUIDE_MCP.md)). Serena
  registers a few dozen tool schemas; the default lazy `mcp_tool_mode` keeps them out of
  the prompt until `mcp_find_tools` asks for them.

## 3. What transfers to lmloop

lmloop's constraints are different from Cursor's: one local model slot, small context
windows, a stdlib-first dependency rule, and memory that stays on the machine. That
reorders the lessons:

| Lesson | Transfers? | Why |
|---|---|---|
| Grep stays the backbone | Yes | Everyone keeps it; it is always fresh |
| Fewer, denser tool results | **Most important** | Each round trip is a local prefill; each result line costs context |
| Structural map within a token budget (Aider) | Yes | No model calls; Python `ast` is stdlib; ctags is optional |
| Symbol definitions and references (LSP, Serena) | Yes, staged | Precise answers in one call; weaker models gain more from them than frontier models |
| Index anchored to git, overlaid with the agent's own writes (Cursor) | Yes, for any index | A stale index that returns "not found" makes the model conclude the symbol does not exist |
| Sparse n-gram regex index | Not now | Solves 15-second `rg` runs in enterprise monorepos; measure before building |
| Embedding the codebase | Still deferred | Cursor's gain depends on a custom-trained embedding model; a generic local embedder competes for the single model slot. Keep the Part 7 rejection until evals show vocabulary-gap misses |

## 4. Recommendations, in build order

### R1. Tighten `search_files` output (small change, immediate)

- Print paths relative to the workspace root, and group hits under one heading per file
  (`rg --heading`), so the path is not repeated on every line.
- Cap the **total** number of matches, not just matches per file. When the cap is hit,
  report `N matches in M files; narrow with glob or path` instead of silently truncating at
  12,000 characters.
- Optionally add `files_only` (`rg -l`) and `count` modes, so a broad first query costs a
  few lines.

### R2. `outline` tool and repo map (new module, stdlib only)

- `outline(path)` returns the symbols in one file with their line ranges, for example
  `class GatePolicy  L164-219`. The model can then call `read_file` with `start_line` and
  `max_lines` instead of paging through 400 lines at a time.
- `repo_map(budget)`: ranked top-level definitions across the repo. Use `ast` for Python
  and universal-ctags (`ctags --output-format=json`) when it is on PATH for other
  languages. With neither available, fall back to listing files only.
- Rank with a file reference graph and personalized PageRank (a ~30-line power iteration;
  NetworkX is not needed). Bias the ranking toward `memory.context.active_context_files`
  and identifiers in the user message. Raw name counts are too noisy: on lmloop, `status`,
  `start`, and `extend` ranked near the top.
- Measured on lmloop: all 68 files parse with `ast` in 0.22 s, and the full public
  outline is about 3,300 tokens, so a ranked 1k-token map is realistic.
- Expose it as a tool, or inject it once at session start. Do not re-inject it every turn:
  that would break the prompt-stable prefix from DESIGN_MEMORY_RETRIEVAL Layer C.

### R3. Persistent symbol table with `find_symbol` and `find_references`

- Store a derived, deletable `code_index.sqlite3` beside the memory index, with the same
  rules: rebuild is always correct, and any failure falls back to scanning.
- Key rows by git blob SHA (`git ls-files -s`), which gives Cursor's Merkle-tree benefit
  for free. For dirty files, use `(mtime_ns, size)`. `write_file`, `update_file`,
  `move_file`, and `delete_file` invalidate their own rows directly, so the agent always
  sees its own writes.
- `find_symbol(name)` returns definition sites from the table. `find_references(name)`
  runs `rg -w` restricted to candidate files and labels each hit with the enclosing symbol
  from the outline, so the model can tell whether a caller is `build_tools` or a test.

### R4. Optional precise backend: LSP or Serena

- Once R3 has shipped and the evals show where name-based lookup fails (overloads,
  methods with common names, re-exports), add an optional LSP backend behind the same
  `find_symbol` and `find_references` tool names. JSON-RPC over stdio needs only the
  stdlib. Alternatively, reach Serena through the MCP client (`lmloop mcp add`). Keep it opt-in per language: language servers are heavy, and pyright on
  a laptop competes with the model for RAM.

### R5. Semantic search (deferred)

- Revisit only if traces show the model repeatedly failing to find code whose vocabulary
  differs from the question ("retry" versus "backoff"). If built, reuse the memory index's
  planned `/v1/embeddings` path through ModelSlots, chunk by the R2 outline boundaries, and
  cache by chunk hash.

### R6. Optional exploration sub-loop

- A read-only `explore` sub-loop (`build_tools(readonly=True)`) that returns a short cited
  summary keeps search noise out of the main context, like Claude Code's Explore subagent.
  It shares the single model slot, so it saves context, not time.

## 5. How to tell whether it worked

Use the existing traces (`~/.lmloop/traces.jsonl`) and golden cases. For each code task,
track the number of `search_files` and `read_file` calls before the first correct edit,
the tool-result tokens, and the pass rate. Add golden cases such as "where is X defined",
"who calls X", and "change X's signature and fix callers", and gate R2 and R3 on those
metrics improving with no regression in `lmloop eval --gate pr`.

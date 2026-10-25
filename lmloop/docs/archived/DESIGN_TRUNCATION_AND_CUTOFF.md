# Design: Truncation and Cutoff — Where `...` Appears and How to Show Full Length

> **Problem:** Text is silently or semi-silently truncated in at least **12 distinct
> pathways** across the codebase. Users see `... [truncated, N chars total]` or
> red `...` in the Live view and cannot easily recover the full content. The
> `/transcript` command itself also truncates in some paths.
>
> **Goal:** Document every truncation site, classify it by *who sees it* (user
> vs. model vs. both), and propose a unified toggle / recovery mechanism so that
> full content is always accessible.
>
> **Status (shipped slice):** `tool_preview_limit`, `diff_limit`,
> `check_output_limit`, `history_preview_chars`, and `max_transcript_tool_chars`
> live in `config.DEFAULTS` and are read with `cfg_positive_int` (the transcript
> key uses `cfg_int`, because `0` means no limit). `/transcript` and
> `/copy transcript` show full tool rows unless `max_transcript_tool_chars` is
> greater than 0.
>
> **Not shipped:** `/show full`, a separate full-vs-truncated tool echo, and
> dropping the 500-character tool clip in `format_messages_transcript()`. That
> text is fed to `/compact` and memory mine, so the clip stays. Current behavior
> is in [ARCHITECTURE.md](../ARCHITECTURE.md).

---

## 1. Inventory of Truncation Sites

### 1.1 Live Streaming Display (User-facing)

| File | Location | What | Why | Severity |
|------|----------|------|-----|----------|
| `display.py:187-226` | `_TailMarkdown._lines()` | Crops markdown render to terminal height, keeping **newest lines** | Terminal viewport; prevents scrollback stacking | **Visible during streaming** — user never sees early content that scrolled off; it's the tail. After `finish()`, the full text is reprinted via `print_markdown()`, so the final output is complete. |
| `display.py:298-307` | `_MarkdownLivePrinter._ensure_live()` | Rich `Live` with `vertical_overflow="crop"` and `auto_refresh=False` | Same as above — deliberate choice over `ellipsis` which paints a red `...` |

**Assessment:** Not a bug — the Live view is a streaming preview. The `finish()` path correctly re-renders the full answer at terminal width. The "cutoff" here is **transient by design** and self-heals.

---

### 1.2 Tool Output to Model (Model-facing, echoed to user)

| File | Location | What | Limit | Configurable? |
|------|----------|------|-------|---------------|
| `tools.py:37-41` | `_TRUNCATE_HINT` | Tool result text sent to model | `MAX_OUTPUT` = `config.DEFAULTS["max_tool_output"]` = **12 000 chars** | ✅ `max_tool_output` in config |
| `tools.py:463-468` | `_clip_preview()` | ⚙-line preview of tool args | **160 chars** (`_TOOL_PREVIEW_LIMIT`) | ❌ hardcoded constant |
| `tools.py:471-486` | `_preview_value()` | Individual arg value in preview | **48 chars** (`_PREVIEW_VALUE_CLIP`) | ❌ hardcoded constant |
| `tools.py:809-817` | `_unified_diff()` | Diff output | **60 lines** (`UPDATE_DIFF_LINES`) | ❌ hardcoded constant |
| `until_runner.py:126-131` | `clip_check_output()` | Shell check output | **2 000 chars** (`CHECK_OUTPUT_LIMIT`) | ❌ hardcoded constant |

---

### 1.3 Web / Extract / Fetch (Model-facing, sometimes echoed to user)

| File | Location | What | Limit | Configurable? |
|------|----------|------|-------|---------------|
| `web.py:65-68` | `_truncate()` | Web fetch body | **8 000 chars** (HTML text), **10 000 chars** (final payload) | ❌ hardcoded |
| `extract.py:402-405` | `_clip()` | PDF/Office/extracted text | **200 000 chars** (`MAX_EXTRACT_CHARS`) | ❌ hardcoded constant |
| `extract.py:172-192` | `attachment_excerpt()` | Inline @file excerpt | **400 lines** (`EXCERPT_MAX_LINES`) | ❌ hardcoded constant |

---

### 1.4 Memory / Transcript / History (User & Model-facing)

| File | Location | What | Limit | Configurable? |
|------|----------|------|-------|---------------|
| `memory.py:648-657` | `_trim_to_max_chars()` | Session transcript for retro/mine | **20 000 chars** (`max_chars` param) | ❌ parameter only |
| `memory.py:817-825` | `session_preview()` | `lmloop history` preview | **120 chars** | ❌ hardcoded |
| `memory.py:708-726` | `format_messages_transcript()` | Live thread for compact/memory mine | **20 000 chars**, tool rows **500 chars** | ❌ parameter only |
| `markdown_view.py:200` | `messages_to_markdown()` | `/copy transcript` & `/transcript` tool content | **4 000 chars** per tool row | ❌ hardcoded |

---

### 1.5 Thinking / Reasoning (User-facing)

| File | Location | What | Limit | Configurable? |
|------|----------|------|-------|---------------|
| `display.py:24` | `_THINK_RECENT_LINES` | Thinking lines kept in `_recent_printed` | **24 lines** | ❌ hardcoded constant |

**Assessment:** This is a display filter, not a data loss — full text is kept in `_ThinkingLive.text()` and available via Ctrl+O.

---

## 2. Classification by Impact

```
Layer           | Affects    | Data Lost? | Reversible?
----------------|------------|------------|-------------
Live streaming  | User       | No*        | Yes — finish() reprints full
Tool output     | Model +    | Yes        | No — model never saw the cut
                | User       |            | part of tool result
Web/extract     | Model      | Yes        | No
Transcript cmd  | User       | Partial    | Yes — fix the limit
History preview | User       | Yes        | No
Config limits   | Model      | Yes        | No — model never saw the cut
```

\* Live streaming is transient — the final output is complete.

---

## 3. Root Cause Analysis

### 3.1 Why Truncation Exists

1. **Context window budget** — Tool output, web fetches, and memory transcripts
   feed into the model's context. Unbounded text would exhaust the window.
2. **Terminal viewport** — Live streaming can't paint unlimited rows.
3. **Performance** — Rendering huge diffs, PDFs, or session histories in the TTY
   or `less` would be slow.
4. **Tool preview readability** — ⚙-lines must fit on one screen row.

### 3.2 Why Truncation Is Problematic

1. **Silent data loss for the user** — Even when the model gets the full output,
   the user only sees the truncated version. If they need to review what the tool
   actually returned, they can't.
2. **`/transcript` itself truncates** — The `messages_to_markdown()` function
   cuts tool content at 4 000 chars, so even the "full session transcript" is
   incomplete for tool-heavy sessions.
3. **No recovery path** — There is no `/show full` or config toggle to expand
   any of these limits.
4. **Hardcoded constants** — Many limits are bare integers with no config hook,
   making it impossible for users to adjust without patching source.

---

## 4. Proposed Design

### 4.1 Principles

1. **Never silently drop data the user asked for.** If a user explicitly requests
   full output (`/transcript`, `/show`, etc.), do not truncate.
2. **Keep model-facing truncation** — The model needs bounded context. Don't
   remove limits that feed the model; instead, provide a parallel path for
   user viewing.
3. **Configurable thresholds** — Every limit that affects user-visible output
   should be tunable via config.
4. **Progressive disclosure** — Default to compact; let the user expand.

### 4.2 Config Keys

Add to `config.DEFAULTS`:

```python
# Truncation / display
"full_tool_output": False,     # Show full tool output to user (not just truncated echo)
"transcript_no_limit": False,  # /transcript and /copy transcript: no char limits
"max_tool_output": 12000,      # Already exists — model-facing limit
"tool_preview_limit": 160,     # New — replace hardcoded _TOOL_PREVIEW_LIMIT
"diff_limit": 60,              # New — replace hardcoded UPDATE_DIFF_LINES
"check_output_limit": 2000,    # New — replace hardcoded CHECK_OUTPUT_LIMIT
"history_preview_chars": 120,  # New — replace hardcoded session_preview limit
```

### 4.3 Code Changes

#### A. Move Hardcoded Constants to Config-Backed Values

```python
# tools.py
_TOOL_PREVIEW_LIMIT = 160
_PREVIEW_VALUE_CLIP = 48
UPDATE_DIFF_LINES = 60

# until_runner.py
CHECK_OUTPUT_LIMIT = 2000

# extract.py
MAX_EXTRACT_CHARS = 200_000
EXCERPT_MAX_LINES = 400
```

Each should accept a config override or at minimum be referenced from a single
module-level location so they can be overridden programmatically.

#### B. `messages_to_markdown()` — Remove 4 000-char Tool Truncation

```python
# markdown_view.py:200
# Current:
parts.append(content[:4000])
# Proposed:
max_tool_in_transcript = cfg.get("transcript_no_limit", False)
if max_tool_in_transcript:
    parts.append(content)
else:
    parts.append(content[:4000])
```

This makes `/transcript` and `/copy transcript` show the full content when the
toggle is set.

#### C. `_trim_to_max_chars()` — Always Keep Full Transcript

```python
# memory.py:648
# format_messages_transcript() and read_session() use max_chars=20000.
# When transcript_no_limit is True, use a very large number or None.
```

#### D. `/show full` REPL Command (Optional UX)

A new slash command that temporarily disables truncation for the current session's
display:

```python
def _cmd_show_full(state, arg):
    """Toggle full output display. /show full on — /show full off"""
    if (arg or "").strip().lower() == "on":
        state.cfg["full_tool_output"] = True
        state.console.info("[full output enabled — tool output, diffs, and transcript shown without truncation]")
    elif (arg or "").strip().lower() == "off":
        state.cfg["full_tool_output"] = False
        state.console.info("[full output disabled — default truncated display]")
    else:
        state.console.info(f"[full output is {'enabled' if state.cfg.get('full_tool_output') else 'disabled'}]")
    return True
```

#### E. Tool Echo Path — Separate "to model" vs "to user"

Currently tool results go to the model (truncated) and are also echoed to the
user (also truncated). Add a path where the full result is available for review:

```python
# tools.py — in each tool that returns text
# Current: return truncated text (sent to model AND echoed to user)
# Proposed: return (full_text, truncated_text)
#   - full_text → stored in session log, available via /transcript
#   - truncated_text → sent to model, echoed to user (default)
# When full_tool_output=True, echo full_text to user too.
```

This is a larger refactor. A simpler interim approach:

**Interim:** Keep current behavior. When `full_tool_output=True`, the tool
echoes the full result to the user's terminal in addition to the truncated
version sent to the model. The session log already stores the full result.

### 4.4 Tiered Approach

| Phase | Scope | Effort |
|-------|-------|--------|
| **P1 — Immediate** | Remove 4 000-char truncation in `messages_to_markdown()` for `/transcript` and `/copy transcript`. Add `transcript_no_limit` config key. | Low — 1 file, 1 line |
| **P1 — not shipped** | Dropping the 500-char clip in `format_messages_transcript()` was rejected: that text feeds `/compact` and memory mine. `max_transcript_tool_chars` applies to `/transcript` only (`0` = full). | — |
| **P2 — Config hooks** | Wire `tool_preview_limit`, `diff_limit`, `check_output_limit` through config. Replace hardcoded constants. | Medium — 4 files |
| **P2 — UX** | Add `/show full` toggle command. | Low — 1 function |
| **P3 — Tool echo** | Separate "to model" vs "to user" output paths in tools. Echo full text when toggle is on. | Medium-High — many tool implementations |
| **P3 — Session log** | Ensure session log JSONL always stores full tool output (not truncated). Audit current behavior. | Investigation |

---

## 5. Detailed File-by-File Change Map

### `lmloop/markdown_view.py`

```diff
@@ -197,7 +197,9 @@ def messages_to_markdown(messages: Iterable[dict]) -> str:
             if not content:
                 continue
             parts.append("### Tool")
             parts.append("")
             parts.append("```")
-            parts.append(content[:4000])
+            parts.append(content)  # No truncation in transcript
             parts.append("```")
             parts.append("")
             continue
```

### `lmloop/memory.py`

```diff
-            lines.append(f"[tool] {content[:500]}")
+            if len(content) > _TRANSCRIPT_TOOL_CHARS:  # 500
+                content = content[:_TRANSCRIPT_TOOL_CHARS] + "..."
+            lines.append(f"[tool] {content}")
```

### `lmloop/tools.py`

```diff
- _TOOL_PREVIEW_LIMIT = 160
+ _TOOL_PREVIEW_LIMIT = 160  # Overridable via config.tool_preview_limit
```

Same treatment for `_PREVIEW_VALUE_CLIP` (48) and `UPDATE_DIFF_LINES` (60).

### `lmloop/until_runner.py`

```diff
- CHECK_OUTPUT_LIMIT = 2000
+ # Overridable via config.check_output_limit
```

### `lmloop/config.py`

Add to `DEFAULTS`:

```python
"transcript_no_limit": False,
"tool_preview_limit": 160,
"diff_limit": 60,
"check_output_limit": 2000,
"history_preview_chars": 120,
```

---

## 6. Edge Cases and Trade-offs

### 6.1 Performance with Full Output

- **`/transcript` on a huge session** — Could be many MB of tool output.
  Mitigation: Paginate through `less` (already done via `page_transcript()`).
  The `less -R` pager handles arbitrarily large input.
- **Tool echo with full output** — A 500 KB tool result echoed to the terminal
  would freeze the TTY. Mitigation: When `full_tool_output=True`, write to a
  temporary file and show a hint: `[full output saved to /tmp/lmloop-tool-XXX.txt]`.

### 6.2 Context Window Impact

- These changes affect **user-facing display only**. The model-facing truncation
  (`max_tool_output`, web fetch limits) stays in place. The model never sees
  unbounded content.

### 6.3 Session Log Integrity

- The session log (`sessions/*.jsonl`) should already store full tool output
  (the tool implementations return text to the caller; the log serializes the
  return value). This needs an audit pass to confirm. If the log stores truncated
  text, ` /transcript` cannot recover what was lost.

### 6.4 `/copy transcript` Size

- Copying a multi-MB transcript to the clipboard may fail on some platforms
  (macOS `pbcopy` has limits). Mitigation: When the output exceeds ~5 MB,
  write to a file and copy the file path instead.

---

## 7. Open Questions

1. **Should `full_tool_output` be a session-level or global config?**
   — Session-level is safer (doesn't affect other projects). But the REPL
   config is project-scoped anyway, so global is effectively session-scoped.

2. **Should we add a `/show tool` command that shows the last tool's full output?**
   — Useful for debugging. Could be part of P2.

3. **Should the `lmloop history` command show a "show full" option?**
   — The 120-char preview is already quite short. A `lmloop history --full` or
   `lmloop history <n> --full` would be a quick win.

4. **Does the session log store truncated or full tool output?**
   — This is the critical audit question. If truncated, no UI change can recover
   the missing data.

---

## 8. Appendix: Complete List of `...` Patterns

| File | Line | Pattern | Meaning |
|------|------|---------|---------|
| `display.py` | 194 | `red ...` in docstring | Describes Rich ellipsis behavior (not used) |
| `display.py` | 291 | `red "..."` in comment | Same as above |
| `tools.py` | 37-41 | `... [truncated, {total} chars total]` | Tool output to model |
| `tools.py` | 468 | `text[:limit-3] + "..."` | Arg preview clip |
| `tools.py` | 482 | `shown[:45] + "..."` | Value preview clip |
| `tools.py` | 816 | `... [diff truncated, N lines total]` | Diff limit |
| `extract.py` | 189 | `... [N lines total; use read_file...]` | Inline excerpt limit |
| `extract.py` | 405 | `... [truncated, N chars total]` | Extracted text clip |
| `web.py` | 60-62 | `... [truncated, {total} chars total]` | Web fetch clip |
| `until_runner.py` | 54 | `... [truncated, {total} chars total]` | Check output clip |
| `memory.py` | 823 | `text[:max_chars] + "..."` | Session preview |

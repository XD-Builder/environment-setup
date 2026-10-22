# Design: Per-category tool concurrency

Shipped. Current behavior is in [ARCHITECTURE.md](../ARCHITECTURE.md).

## Decision

`ToolDef.category` is the only classification. There is no second name list.
`concurrent=False` tools (`run_shell`, file writes, memory writes) stay serial
barriers. `concurrent_groups()` bundles consecutive concurrent calls into one
wave. Inside the wave, `run_tool_calls()` starts one thread pool per category
and joins them before the next barrier. Results stay in call order.

- `local_read` — `read_file`, `list_dir`, `find_files`, `search_files`,
  `current_time`, `recall_memory`. Config `local_read_concurrency`, default 8.
- `web_search` — `web_search`. Config `web_search_concurrency`, default 2.
  DuckDuckGo starts returning CAPTCHAs after a few rapid requests.
- `web_fetch` — `fetch_url`. Config `web_fetch_concurrency`, default 4.

A non-positive config value falls back to that default (`cfg_positive_int`).
Keys are flat, like the rest of `config.DEFAULTS`.

Separate pools, not one shared pool plus a semaphore: a task blocked on a
semaphore still occupies a worker, so reads would wait behind queued searches.

## Not done

No process-global backend rotation. `web_search` still tries ddgs, then
DuckDuckGo HTML, Lite, Instant Answer, and Wikipedia when a backend fails.
The in-flight cap is the CAPTCHA control. Rotating the primary backend would
skip ddgs on later calls, including serial ones, and can make Wikipedia the
first try.

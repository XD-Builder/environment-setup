# Time — relative windows

The Clock block (and `current_time`) is the source of truth for "now".
Your training cutoff is not. Years like 2024 or 2025 are not "recent".

## Iron law

If the user mentions a relative window — "last 4 weeks", "past month",
"today", "this year", "recently", "yesterday" — resolve start and end
as ISO dates **before** `web_search` or `fetch_url`.

1. Read Clock (`today` / UTC). If the session may be stale, call `current_time`.
2. Convert the window (4 weeks → `28_days_ago` through `today`).
3. Put those `YYYY-MM-DD` dates in the search query. Use `after:` / a year
   only when they match that range.
4. Discard hits whose dates fall outside the window. Say so if sources are undated.

Do not start research from a cutoff year because it "feels recent".

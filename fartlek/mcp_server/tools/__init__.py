"""Tool implementations.

Each module exposes one async entry point taking (ctx: ToolContext, ...params)
and returning the rendered markdown string. Shared rules:

- Build a render.renderer.Report; set report.banner = ctx.banner(); call
  render(report, CAP) with the tool's hard cap. The caps live in each module
  (`CAP` / `CAP_TOKENS`) and are summed by tests/test_guardrails.py.
- Breadcrumbs (report.next_steps) and error text may reference ONLY registered
  tools with declared parameters (CI-enforced, tests/test_guardrails.py).
- Errors are corrective, never bare: name valid formats, today's
  date, nearest alternatives with real IDs. Auth failures are converted to
  fixed strings by server._guard (AUTH_ERROR, NOT_CONFIGURED, CONNECT_MISSING).
- Where a tool needs data a Garmin export does not hold, it checks
  ctx.source_kind ("connect", "export" or "store") and says so, rather than
  reporting a failed live fetch.
- Dates render as Ddd YYYY-MM-DD (renderer.format_date). Numbers are
  pre-formatted (pace mm:ss/km, durations h:mm). Verdict strength gated by
  confidence — provisional data ⇒ PROVISIONAL-prefixed verdict.
"""

"""Phase-1 tool implementations.

Each module exposes one async entry point taking (ctx: ToolContext, ...params)
and returning the rendered markdown string. Shared rules (DESIGN §2.3/§4/§5):

- Build a render.renderer.Report; set report.banner = ctx.banner(); call
  render(report, CAP) with the tool's hard cap. Caps (tokens): brief 600 ·
  activities 1300 · activity 1000/2000/4000 (standard/splits/full) ·
  athlete 600 · set_profile 200 · log 120 · sync 150 · raw 5000.
- Breadcrumbs (report.next_steps) and error text may reference ONLY shipped
  tools with declared parameters (CI-enforced): garmin_brief,
  garmin_activities, garmin_activity, garmin_athlete, garmin_set_profile,
  garmin_log, garmin_sync, garmin_raw. Phase-2 tools do not exist yet —
  never mention them.
- Errors are corrective, never bare (§4.3): name valid formats, today's
  date, nearest alternatives with real IDs. Auth failure returns the fixed
  string: "Garmin session expired — the user must re-run `fartlek auth`.
  Retrying will not help."
- Dates render as Ddd YYYY-MM-DD (renderer.format_date). Numbers are
  pre-formatted (pace mm:ss/km, durations h:mm). Verdict strength gated by
  confidence (§5 rule 5) — provisional data ⇒ PROVISIONAL-prefixed verdict.
"""

"""garmin_sync — forced refresh / historical backfill (DESIGN §2.4, cap 150).

Delegates the fetch to ctx.run_sync(backfill_days) (inline incremental(),
plus tier2(backfill_days) when requested — the engine's sync.lock guards
concurrency). Reports freshness before/after (sync_state['last_sync']),
calls made, new activities, nights backfilled, and passes the engine's
"skipped: another sync holds the lock" state through. Short plain string,
banner-prefixed when an alert is active (§4.4).

Stats keys read tolerantly from the merged dict: calls, new_activities,
nights, done, errors, skipped/reason.
"""
from __future__ import annotations

from datetime import datetime

from fartlek.render.renderer import estimate_tokens, format_date

CAP_TOKENS = 150


def _finish(banner: str | None, body: str, cap: int = CAP_TOKENS) -> str:
    text = f"{banner}\n\n{body}" if banner else body
    if estimate_tokens(text) > cap:
        text = text[: int(cap * 3.2) - 2].rstrip() + " …"
    return text


def _fmt_ts(iso: str | None) -> str | None:
    """'2026-07-20T07:41:23' → 'Mon 2026-07-20 07:41'."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    return f"{format_date(dt.date().isoformat())} {dt.strftime('%H:%M')}"


async def run(ctx, backfill_days: int = 0) -> str:
    await ctx.ensure_ready()
    banner = ctx.banner()

    if backfill_days < 0:
        return _finish(
            banner,
            f"backfill_days must be ≥0 (got {backfill_days}). "
            "Example: garmin_sync(backfill_days=60)",
        )

    before = ctx.store.get_sync_state("last_sync")
    stats = await ctx.run_sync(backfill_days)

    if stats.get("skipped"):
        last = _fmt_ts(before) or "never"
        return _finish(
            banner,
            "Sync skipped: another sync in progress — data unchanged "
            f"(last sync {last}). Retry in a minute.",
        )

    after = ctx.store.get_sync_state("last_sync")
    calls = int(stats.get("calls", 0))
    new_acts = int(stats.get("new_activities", 0))

    bits = [
        f"last sync {_fmt_ts(before) or 'never'} → {_fmt_ts(after) or 'unknown'}",
        f"{calls} calls",
        f"{new_acts} new " + ("activity" if new_acts == 1 else "activities"),
    ]
    if backfill_days > 0:
        nights = int(stats.get("nights", 0))
        night_bit = f"{nights} nights backfilled"
        if stats.get("done") is False:
            night_bit += f" (more remain — garmin_sync(backfill_days={backfill_days}) to continue)"
        bits.append(night_bit)
    errors = stats.get("errors") or []
    if errors:
        bits.append(f"{len(errors)} endpoint errors (non-fatal)")

    return _finish(banner, "Synced: " + " · ".join(bits) + ".")

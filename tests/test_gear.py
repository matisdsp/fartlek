"""Gear wear and rotation analytics (DESIGN.md §3.2)."""
from __future__ import annotations

from fartlek.analytics.gear import (
    DUE_FRACTION,
    WATCH_FRACTION,
    GearStatus,
    assess,
    headline,
)

TODAY = "2026-08-06"


def row(uuid, name, *, total=None, max_m=1_000_000.0, type="shoes", **kw):
    r = {"uuid": uuid, "name": name, "type": type, "status": "active",
         "make_model": None, "total_meters": total, "max_meters": max_m,
         "total_activities": None}
    r.update(kw)
    return r


def usage(**by_uuid):
    """uuid=(metres, sessions) → the store.gear_usage shape."""
    return {
        u: {"uuid": u, "meters": m, "sessions": s, "last_used": None}
        for u, (m, s) in by_uuid.items()
    }


# --- verdicts ----------------------------------------------------------------

def test_verdict_thresholds_are_fractions_of_the_athletes_own_limit():
    rows = [
        row("a", "Fresh", total=100_000.0),          # 10%
        row("b", "Watched", total=760_000.0),        # 76%
        row("c", "Done", total=1_010_000.0),         # 101%
    ]
    by_uuid = {g.uuid: g for g in assess(rows, {}, {}, TODAY)}
    assert by_uuid["a"].verdict == "ok"
    assert by_uuid["b"].verdict == "watch"
    assert by_uuid["c"].verdict == "due"


def test_verdict_boundaries_are_inclusive():
    rows = [
        row("a", "At watch", total=WATCH_FRACTION * 1_000_000.0),
        row("b", "At due", total=DUE_FRACTION * 1_000_000.0),
    ]
    by_uuid = {g.uuid: g for g in assess(rows, {}, {}, TODAY)}
    assert by_uuid["a"].verdict == "watch"
    assert by_uuid["b"].verdict == "due"


def test_no_threshold_is_unknown_not_a_guessed_default():
    """Fartlek publishes no universal shoe lifespan; without the athlete's own
    limit there is nothing to judge the mileage against."""
    [g] = assess([row("a", "Unlimited", total=900_000.0, max_m=None)], {}, {}, TODAY)
    assert g.verdict == "unknown" and g.fraction is None
    assert g.total_km == 900.0 and g.threshold_km is None


def test_missing_odometer_is_unknown_too():
    [g] = assess([row("a", "Never synced", total=None)], {}, {}, TODAY)
    assert g.verdict == "unknown" and g.total_km is None


# --- ordering ----------------------------------------------------------------

def test_worst_first_then_most_worn():
    rows = [
        row("ok", "Fresh", total=50_000.0),
        row("due1", "Old", total=1_010_000.0),
        row("watch", "Ageing", total=800_000.0),
        row("due2", "Older", total=1_400_000.0),
        row("none", "No limit", total=10_000.0, max_m=None),
    ]
    assert [g.uuid for g in assess(rows, {}, {}, TODAY)] == [
        "due2", "due1", "watch", "ok", "none"
    ]


# --- rotation ----------------------------------------------------------------

def test_share_is_computed_within_a_gear_type():
    """A bike's kilometres must not swamp every shoe in the locker."""
    rows = [
        row("s1", "Shoe A"), row("s2", "Shoe B"),
        row("b1", "Bike", type="bike"),
    ]
    got = {g.uuid: g for g in assess(
        rows, usage(s1=(150_000.0, 10), s2=(50_000.0, 4), b1=(900_000.0, 12)),
        {}, TODAY,
    )}
    assert got["s1"].share == 0.75 and got["s2"].share == 0.25
    assert got["b1"].share == 1.0
    assert got["s1"].window_km == 150.0 and got["s1"].window_sessions == 10


def test_share_is_none_when_nothing_was_attributed():
    [g] = assess([row("a", "Unused")], {}, {}, TODAY)
    assert g.share is None and g.window_km == 0.0 and g.window_sessions == 0


def test_window_km_is_local_attribution_not_the_odometer():
    """The odometer counts sessions older than this store's history window;
    the window figure is only what the links can prove."""
    [g] = assess([row("a", "Pair", total=900_000.0)], usage(a=(120_000.0, 8)),
                 {}, TODAY)
    assert g.total_km == 900.0 and g.window_km == 120.0


def test_last_used_spans_the_whole_store_and_yields_days_since():
    [g] = assess([row("a", "Pair")], {}, {"a": "2026-07-30"}, TODAY)
    assert g.last_used == "2026-07-30" and g.days_since == 7
    [never] = assess([row("b", "Unworn")], {}, {}, TODAY)
    assert never.last_used is None and never.days_since is None


# --- headline ----------------------------------------------------------------

def test_headline_leads_with_the_pair_that_needs_a_decision():
    rows = [row("a", "Bondi 9 I", total=1_012_000.0), row("b", "Fresh", total=10_000.0)]
    line = headline(assess(rows, {}, {}, TODAY))
    assert "Bondi 9 I" in line and "1012 km" in line and "replace" in line


def test_headline_counts_the_other_pairs_past_their_limit():
    rows = [row("a", "One", total=1_100_000.0), row("b", "Two", total=1_050_000.0)]
    assert "+1 more past their limit" in headline(assess(rows, {}, {}, TODAY))


def test_headline_watch_reports_the_percentage():
    line = headline(assess([row("a", "Ageing", total=800_000.0)], {}, {}, TODAY))
    assert "80%" in line and "plan a replacement" in line


def test_headline_says_so_when_nothing_needs_attention():
    rows = [row("a", "One", total=100_000.0), row("b", "Two", total=200_000.0)]
    assert headline(assess(rows, {}, {}, TODAY)) == (
        "2 items on file, none near its retirement limit"
    )


def test_headline_distinguishes_no_limit_set_from_all_clear():
    rows = [row("a", "One", total=900_000.0, max_m=None)]
    line = headline(assess(rows, {}, {}, TODAY))
    assert "no limit to judge it against" in line
    assert "Garmin Connect" in line  # says where to set one


def test_headline_on_an_empty_locker():
    assert headline([]) == "no gear on file"


def test_assess_returns_gear_status_objects():
    [g] = assess([row("a", "Pair", make_model="Hoka Bondi 9")], {}, {}, TODAY)
    assert isinstance(g, GearStatus)
    assert g.make_model == "Hoka Bondi 9" and g.status == "active"

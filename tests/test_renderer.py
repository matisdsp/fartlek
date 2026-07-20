"""Renderer tests (DESIGN.md §5 + §4.4): drop order, disclosures, invariants."""
from __future__ import annotations

import pytest

from fartlek.render.renderer import (
    Report,
    Row,
    Section,
    arrow_series,
    estimate_tokens,
    format_banner,
    format_date,
    render,
)

BIG_CAP = 100_000


# ---------------------------------------------------------------------------
# estimator / formatting primitives
# ---------------------------------------------------------------------------

def test_estimator_sanity():
    assert estimate_tokens("x" * 32) == 10
    assert estimate_tokens("") == 0
    assert estimate_tokens("x") == 1  # ceil, never floor


def test_format_date():
    # Real calendar weekdays (DESIGN.md's sample dates are fictional).
    assert format_date("2026-07-19") == "Sun 2026-07-19"
    assert format_date("2026-07-20") == "Mon 2026-07-20"
    assert format_date("2026-07-14") == "Tue 2026-07-14"


def test_arrow_series_no_downsample():
    assert arrow_series([1, 2, 3]) == "1→2→3"


def test_arrow_series_endpoints_preserved():
    out = arrow_series([float(v) for v in range(100)], max_points=12)
    parts = out.split("→")
    assert len(parts) == 12
    assert parts[0] == "0"
    assert parts[-1] == "99"


def test_arrow_series_fmt_and_edges():
    assert arrow_series([1.24, 5.68], fmt="{:.1f}") == "1.2→5.7"
    assert arrow_series([]) == ""
    assert arrow_series([7.0]) == "7"
    assert arrow_series([1.0, 2.0, 3.0], max_points=1) == "3"


# ---------------------------------------------------------------------------
# format_banner
# ---------------------------------------------------------------------------

def _alert(date, severity, message):
    return {"date": date, "severity": severity, "message": message}


def test_banner_none_without_red_amber():
    assert format_banner([]) is None
    assert format_banner([_alert("2026-07-17", "WATCH", "deep sleep low")]) is None


def test_banner_single_red():
    out = format_banner([_alert("2026-07-16", "RED", "HRV below band 3 days")])
    assert out == "⚠ ACTIVE (since Thu 07-16): HRV below band 3 days — see garmin_recovery()"


def test_banner_joins_two_highest_severity_and_counts_rest():
    out = format_banner([
        _alert("2026-07-16", "AMBER", "RHR +5"),
        _alert("2026-07-17", "RED", "HRV below band 3 days"),
        _alert("2026-07-18", "AMBER", "sleep debt rising"),
        _alert("2026-07-19", "WATCH", "never banners"),
    ])
    # RED outranks AMBER regardless of input order; since = earliest RED/AMBER date.
    assert out == (
        "⚠ ACTIVE (since Thu 07-16): HRV below band 3 days + RHR +5 +1 more"
        " — see garmin_recovery()"
    )
    assert "never banners" not in out


# ---------------------------------------------------------------------------
# render: golden small report
# ---------------------------------------------------------------------------

def _small_report():
    return Report(
        title="Daily Brief",
        date="2026-07-19",
        data_as_of="07:41",
        verdict="GREEN — cleared for quality.",
        sections=[
            Section(
                title="Signals",
                header=["Signal", "Today", "Flag"],
                rows=[Row(["HRV", "97 ms", "ok"]), Row(["RHR", "44 bpm", "ok"])],
                method_note="population band",
            ),
            Section(title=None, header=None,
                    prose="Yesterday: Run 12.0 km easy (id 19501244)."),
        ],
        watch_list=["deep sleep low 2 nights"],
        next_steps=["garmin_activity(activity_id=19501244)", "garmin_week()"],
    )


GOLDEN = """\
# Daily Brief — Sun 2026-07-19 (data as of 07:41)

**VERDICT: GREEN — cleared for quality.**

**Signals**
| Signal | Today | Flag |
|---|---|---|
| HRV | 97 ms | ok |
| RHR | 44 bpm | ok |
(population band)

Yesterday: Run 12.0 km easy (id 19501244).

Watch:
- deep sleep low 2 nights

Next: garmin_activity(activity_id=19501244) · garmin_week()"""


def test_golden_under_cap_unchanged():
    assert render(_small_report(), 400) == GOLDEN


def test_render_does_not_mutate_report():
    r = _small_report()
    render(r, 60)  # forces drops
    assert len(r.sections[0].rows) == 2
    assert render(r, 400) == GOLDEN


# ---------------------------------------------------------------------------
# render: drop order on an oversized report
# ---------------------------------------------------------------------------

def _big_report(undroppable_idx: int | None = None):
    pad = "x" * 40
    rows = [
        Row([f"Sun 07-{20 - i:02d}", f"session-{i + 1:02d} {pad}", "50"],
            undroppable=(i == undroppable_idx))
        for i in range(10)  # most-recent-first
    ]
    return Report(
        title="Week",
        date="2026-07-20",
        data_as_of="07:41",
        verdict="a good, absorbable week — load +8% on a sustainable ramp.",
        sections=[
            Section(
                title="Recent sessions",
                header=["Date", "Session", "Load"],
                rows=rows,
                method_note="splits-based; hot-day sessions excluded; "
                            "mapped from configured zones " + pad,
            ),
            Section(
                title="Sleep detail",
                header=["Night", "Score"],
                rows=[Row([f"night-{i} {pad}", "66"]) for i in range(2)],
                priority="secondary",
                overflow_hint="garmin_recovery(days=28) for all",
            ),
        ],
        watch_list=[f"watch-item-{i} needs monitoring closely" for i in range(5)],
        next_steps=["garmin_week()", "garmin_recovery(days=14)"],
    )


def test_watch_list_capped_at_three_even_under_cap():
    out = render(_big_report(), BIG_CAP)
    assert "Watch:" in out
    assert out.count("\n- ") == 3
    assert "watch-item-2" in out and "watch-item-3" not in out
    assert "(2 more omitted)" in out


def test_drop_order():
    r = _big_report()
    full = render(r, BIG_CAP)
    assert "session-10" in full and "(more rows" not in full

    # ① rows beyond the 6 most recent, disclosed with the generic notice
    cap1 = estimate_tokens(full) - 1
    r1 = render(r, cap1)
    assert estimate_tokens(r1) <= cap1
    assert "session-06" in r1 and "session-07" not in r1
    assert "(4 more rows omitted)" in r1
    assert "night-0" in r1                  # secondary intact
    assert "(splits-based" in r1            # method note intact
    assert "watch-item-2" in r1

    # ② whole secondary section, disclosed via its overflow_hint
    cap2 = estimate_tokens(r1) - 1
    r2 = render(r, cap2)
    assert estimate_tokens(r2) <= cap2
    assert "night-0" not in r2
    assert "(Sleep detail omitted — garmin_recovery(days=28) for all)" in r2
    assert "session-06" in r2               # primary still at 6 rows
    assert "(splits-based" in r2            # ② before ④

    # ④ method parentheticals (③ is structural: watch already ≤3)
    cap3 = estimate_tokens(r2) - 1
    r3 = render(r, cap3)
    assert estimate_tokens(r3) <= cap3
    assert "(splits-based" not in r3
    assert "(method notes omitted)" in r3
    assert "session-06" in r3               # ④ before the last-resort row trim

    # last resort: primary rows beyond the 3 most recent
    cap4 = estimate_tokens(r3) - 1
    r4 = render(r, cap4)
    assert estimate_tokens(r4) <= cap4
    assert "session-03" in r4 and "session-04" not in r4
    assert "(7 more rows omitted)" in r4    # 4 from ① + 3 more

    # then the remaining watch list
    cap5 = estimate_tokens(r4) - 1
    r5 = render(r, cap5)
    assert estimate_tokens(r5) <= cap5
    assert "watch-item-0" not in r5
    assert "(watch list omitted)" in r5


def test_undroppable_rows_survive_row_trim():
    r = _big_report(undroppable_idx=8)      # session-09, beyond the 6 most recent
    full = render(r, BIG_CAP)
    out = render(r, estimate_tokens(full) - 1)
    assert "session-09" in out
    assert "session-07" not in out and "session-10" not in out
    assert "(3 more rows omitted)" in out   # 4 beyond head minus 1 undroppable


def test_generic_vs_hint_disclosure():
    r = _big_report()
    r.sections[0].overflow_hint = "garmin_activities(start_date='2026-07-07') for all"
    full = render(r, BIG_CAP)
    out = render(r, estimate_tokens(full) - 1)
    assert "(4 more rows — garmin_activities(start_date='2026-07-07') for all)" in out


# ---------------------------------------------------------------------------
# render: skeleton invariants
# ---------------------------------------------------------------------------

def test_banner_survives_extreme_cap():
    banner = "⚠ ACTIVE (since Thu 07-16): HRV below band 3 days — see garmin_recovery()"
    r = _big_report()
    r.banner = banner
    skeleton = render(
        Report(title=r.title, date=r.date, data_as_of=r.data_as_of,
               verdict=r.verdict, banner=banner, next_steps=r.next_steps),
        BIG_CAP,
    )
    out = render(r, estimate_tokens(skeleton))
    assert out.startswith(banner)
    assert "# Week — Mon 2026-07-20 (data as of 07:41)" in out
    assert "**VERDICT:" in out
    assert out.endswith("Next: garmin_week() · garmin_recovery(days=14)")
    assert "session-" not in out and "watch-item" not in out and "night-" not in out


def test_value_error_when_skeleton_exceeds_cap():
    with pytest.raises(ValueError):
        render(_small_report(), 1)


def test_provisional_assertions():
    r = _small_report()
    r.provisional = True                    # verdict lacks the required prefix
    with pytest.raises(AssertionError):
        render(r, BIG_CAP)

    r.verdict = "PROVISIONAL (n=12 of 42 days) — leaning GREEN"
    out = render(r, BIG_CAP)
    assert "**VERDICT: PROVISIONAL (n=12 of 42 days) — leaning GREEN**" in out

    r.provisional = False                   # bare report claiming PROVISIONAL
    with pytest.raises(AssertionError):
        render(r, BIG_CAP)

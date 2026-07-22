"""Sleep debt, SRI and social jetlag (DESIGN.md §3.2 #10).

SRI is checked against hand-constructed patterns whose answer is knowable
without running the code: identical nights must score 100, and inverted nights
must score -100. Anything in between is only meaningful if those two anchors
hold.
"""
from __future__ import annotations

import json

import pytest

from fartlek.analytics import sleep as sl


def night(wake_date, *, start="23:00", end="07:00", awake_windows=()):
    """One night's timeline row: a single sleep block, minus any awake windows.

    Times are local clock strings; a start >= 12:00 belongs to the evening
    before `wake_date`.
    """
    from datetime import date, datetime, timedelta

    wake = date.fromisoformat(wake_date)
    sh, sm = (int(x) for x in start.split(":"))
    eh, em = (int(x) for x in end.split(":"))
    start_day = wake - timedelta(days=1) if sh >= 12 else wake
    s = datetime.combine(start_day, datetime.min.time()) + timedelta(hours=sh, minutes=sm)
    e = datetime.combine(wake, datetime.min.time()) + timedelta(hours=eh, minutes=em)

    cuts = []
    for a_start, a_end in awake_windows:
        ah, am = (int(x) for x in a_start.split(":"))
        bh, bm = (int(x) for x in a_end.split(":"))
        a_day = wake - timedelta(days=1) if ah >= 12 else wake
        b_day = wake - timedelta(days=1) if bh >= 12 else wake
        cuts.append((
            datetime.combine(a_day, datetime.min.time()) + timedelta(hours=ah, minutes=am),
            datetime.combine(b_day, datetime.min.time()) + timedelta(hours=bh, minutes=bm),
        ))

    intervals, cursor = [], s
    for a, b in sorted(cuts):
        if a > cursor:
            intervals.append(["light", cursor.isoformat(), a.isoformat()])
        intervals.append(["awake", a.isoformat(), b.isoformat()])
        cursor = b
    if cursor < e:
        intervals.append(["light", cursor.isoformat(), e.isoformat()])
    return {"date": wake_date, "intervals_json": json.dumps(intervals)}


def nights(start_date, n, **kw):
    from datetime import date, timedelta
    d0 = date.fromisoformat(start_date)
    return [night((d0 + timedelta(days=i)).isoformat(), **kw) for i in range(n)]


# --- timeline parsing -------------------------------------------------------

def test_malformed_intervals_are_skipped_not_fatal():
    assert sl.parse_intervals("not json") == []
    assert sl.parse_intervals(json.dumps([["light", "nope", "also-nope"]])) == []
    assert sl.parse_intervals(json.dumps([["light"]])) == []


def test_occupancy_splits_a_night_across_midnight():
    grid = sl.occupancy_grid([night("2026-05-02", start="23:00", end="07:00")])
    assert set(grid) == {"2026-05-01", "2026-05-02"}
    assert sum(grid["2026-05-01"]) == 60       # 23:00-24:00
    assert sum(grid["2026-05-02"]) == 7 * 60   # 00:00-07:00


def test_intra_sleep_wakings_count_as_wake():
    """Phillips defines the state as sleep/wake: a night broken into wakings
    genuinely is less regular than an unbroken one."""
    grid = sl.occupancy_grid([
        night("2026-05-02", start="23:00", end="07:00", awake_windows=[("03:00", "04:00")])
    ])
    assert sum(grid["2026-05-02"]) == 6 * 60   # 7h span minus the 1h waking


# --- SRI --------------------------------------------------------------------

def test_identical_nights_score_100():
    res = sl.sleep_regularity_index(nights("2026-05-01", 10), "2026-05-10", days=10)
    assert res["suppressed"] is False
    assert res["sri"] == pytest.approx(100.0)


def test_inverted_pattern_scores_minus_100():
    """Sleeping the exact complement of the previous day is maximal
    irregularity — the formula's lower bound, and proof the scale is the
    Phillips -100..100 one rather than a 0..100 rescaling.

    Built from raw intervals: the `night` helper models ordinary overnight
    sleep and cannot express "asleep all afternoon".
    """
    rows = []
    for i in range(1, 10):
        d = f"2026-05-{i:02d}"
        block = (("00:00", "12:00") if i % 2 else ("12:00", "23:59"))
        rows.append({
            "date": d,
            "intervals_json": json.dumps(
                [["light", f"{d}T{block[0]}:00", f"{d}T{block[1]}:00"]]
            ),
        })
    res = sl.sleep_regularity_index(rows, "2026-05-09", days=9)
    assert res["suppressed"] is False
    assert res["sri"] is not None and res["sri"] < -95


def test_shifted_bedtimes_score_between_the_anchors():
    from datetime import date, timedelta
    rows = []
    d0 = date.fromisoformat("2026-05-01")
    for i in range(10):
        start = "23:00" if i % 2 == 0 else "01:00"
        end = "07:00" if i % 2 == 0 else "09:00"
        rows.append(night((d0 + timedelta(days=i)).isoformat(), start=start, end=end))
    res = sl.sleep_regularity_index(rows, "2026-05-10", days=10)
    assert -100 < res["sri"] < 100


def test_sri_suppressed_below_the_minimum_pairs():
    res = sl.sleep_regularity_index(nights("2026-05-01", 3), "2026-05-03", days=7)
    assert res["suppressed"] is True and res["sri"] is None
    assert "need" in res["reason"]


def test_missing_nights_are_skipped_not_treated_as_wakefulness():
    """A watch left on the charger must not read as an irregular sleeper.

    The gap costs two comparable transitions (the day before it and the day
    itself), so the window has to be long enough to keep clearing the minimum.
    """
    rows = nights("2026-05-01", 14)
    del rows[6]                       # one night missing
    res = sl.sleep_regularity_index(rows, "2026-05-14", days=14)
    assert res["suppressed"] is False
    assert res["sri"] == pytest.approx(100.0)   # remaining pairs are still identical
    assert res["n_pairs"] < 13


def test_sri_respects_the_window():
    rows = nights("2026-04-01", 30)
    res = sl.sleep_regularity_index(rows, "2026-04-30", days=7)
    assert res["n_pairs"] <= 6


# --- debt -------------------------------------------------------------------

def day(d, *, actual=7.0, need=8.0):
    return {"date": d, "sleep_duration_h": actual, "sleep_need_h": need}


def test_debt_sums_shortfalls_only():
    rows = [day("2026-05-01", actual=6.0, need=8.0),   # -2
            day("2026-05-02", actual=7.5, need=8.0)]   # -0.5
    res = sl.sleep_debt(rows, "2026-05-02", window=14)
    assert res["debt_h"] == pytest.approx(2.5)
    assert res["nights"] == 2 and res["nights_short"] == 2


def test_surplus_nights_do_not_cancel_debt():
    """One long Sunday must not erase a hard week — debt is not a balance."""
    rows = [day("2026-05-01", actual=5.0, need=8.0),    # -3
            day("2026-05-02", actual=11.0, need=8.0)]   # +3, ignored
    assert sl.sleep_debt(rows, "2026-05-02")["debt_h"] == pytest.approx(3.0)


def test_need_source_is_disclosed():
    device = [day("2026-05-01", need=9.0), day("2026-05-02", need=9.0)]
    assert sl.sleep_debt(device, "2026-05-02")["need_source"] == "device"

    fallback = [{"date": "2026-05-01", "sleep_duration_h": 7.0, "sleep_need_h": None},
                {"date": "2026-05-02", "sleep_duration_h": 7.0, "sleep_need_h": None}]
    res = sl.sleep_debt(fallback, "2026-05-02")
    assert res["need_source"] == "default"
    assert res["debt_h"] == pytest.approx(2 * (sl.DEFAULT_SLEEP_NEED_H - 7.0))

    mixed = [day("2026-05-01", need=9.0),
             {"date": "2026-05-02", "sleep_duration_h": 7.0, "sleep_need_h": None}]
    assert sl.sleep_debt(mixed, "2026-05-02")["need_source"] == "mixed"


def test_debt_window_excludes_older_nights():
    rows = [day("2026-04-01", actual=4.0), day("2026-05-02", actual=7.0)]
    res = sl.sleep_debt(rows, "2026-05-02", window=14)
    assert res["nights"] == 1 and res["debt_h"] == pytest.approx(1.0)


def test_debt_with_no_data_returns_none_not_zero():
    """Zero debt and no data are opposite claims."""
    res = sl.sleep_debt([], "2026-05-02")
    assert res["debt_h"] is None and res["nights"] == 0


def test_nights_without_duration_are_ignored():
    rows = [{"date": "2026-05-01", "sleep_duration_h": None, "sleep_need_h": 8.0},
            day("2026-05-02", actual=6.0)]
    assert sl.sleep_debt(rows, "2026-05-02")["nights"] == 1


# --- social jetlag ----------------------------------------------------------

def sleep_row(d, start_iso, end_iso):
    return {"date": d, "sleep_start_ts": start_iso, "sleep_end_ts": end_iso}


def test_jetlag_is_positive_when_weekends_run_later():
    # 2026-05-04 is a Monday; 05-09 Sat, 05-10 Sun.
    # Weekdays: 23:00 -> 07:00, mid-sleep 03:00.
    # Weekends: 02:00 -> 10:00 (both on the wake date), mid-sleep 06:00.
    rows = [sleep_row(f"2026-05-{d:02d}", f"2026-05-{d - 1:02d}T23:00:00",
                      f"2026-05-{d:02d}T07:00:00") for d in (4, 5, 6, 7, 8)]
    rows += [sleep_row(f"2026-05-{d:02d}", f"2026-05-{d:02d}T02:00:00",
                       f"2026-05-{d:02d}T10:00:00") for d in (9, 10)]
    res = sl.social_jetlag(rows, "2026-05-10", window=14)
    assert res["suppressed"] is False
    assert res["jetlag_h"] == pytest.approx(3.0)
    assert res["n_weekday"] == 5 and res["n_weekend"] == 2


def test_midpoint_axis_does_not_wrap_around_midnight():
    """A 23:00 and an 03:00 mid-sleep must average sensibly, not to midday."""
    late = sl._mid_sleep_hours(sleep_row("2026-05-05", "2026-05-04T22:00:00",
                                         "2026-05-05T04:00:00"))
    assert sl.format_clock(late) == "01:00"
    early = sl._mid_sleep_hours(sleep_row("2026-05-05", "2026-05-04T20:00:00",
                                          "2026-05-05T02:00:00"))
    assert sl.format_clock(early) == "23:00"
    assert late > early


def test_jetlag_suppressed_without_both_groups():
    rows = [sleep_row(f"2026-05-{d:02d}", f"2026-05-{d - 1:02d}T23:00:00",
                      f"2026-05-{d:02d}T07:00:00") for d in (4, 5, 6)]
    res = sl.social_jetlag(rows, "2026-05-06")
    assert res["suppressed"] is True and res["jetlag_h"] is None


def test_unusable_timestamps_are_skipped():
    assert sl._mid_sleep_hours(sleep_row("2026-05-05", None, None)) is None
    assert sl._mid_sleep_hours(sleep_row("2026-05-05", "bad", "worse")) is None
    # end before start (corrupt row) must not produce a negative-length night
    assert sl._mid_sleep_hours(
        sleep_row("2026-05-05", "2026-05-05T08:00:00", "2026-05-05T07:00:00")
    ) is None

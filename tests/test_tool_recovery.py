"""Tests for the garmin_recovery tool.

Hermetic: seeded temp Store behind a FakeContext, no real ToolContext. The
verdict is convergence's output, so these tests check SELECTION and PHRASING —
that absent markers produce no row, that the cap holds, and that the athlete's
own report still outranks calm sensors once it reaches the rendered page.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import date, timedelta

import pytest

from fartlek.mcp_server.tools import recovery
from fartlek.render.renderer import estimate_tokens
from tests.conftest import make_days

TODAY = "2026-07-20"
TS = "2026-07-20T08:00:00"


class FakeContext:
    def __init__(self, store, today: str = TODAY, banner: str | None = None):
        self._store = store
        self._today = today
        self._banner = banner
        self.ready_calls = 0

    @property
    def store(self):
        return self._store

    async def ensure_ready(self) -> None:
        self.ready_calls += 1

    def today(self) -> str:
        return self._today

    def data_as_of(self) -> str:
        return "07:41"

    def banner(self) -> str | None:
        return self._banner


def run(ctx, **kw) -> str:
    return asyncio.run(recovery.run(ctx, **kw))


def seed(store, *, nights=60, hrv=88.0, rhr=46.0, sleep_h=7.8, need_h=8.0,
         deep_h=1.2, load=80.0, sleep_score=72):
    rows = make_days(TODAY, nights, hrv_last_night=hrv, resting_hr=rhr,
                     sleep_duration_h=sleep_h, sleep_need_h=need_h,
                     sleep_deep_h=deep_h, daily_load=load, sleep_score=sleep_score)
    for r in rows:
        store.upsert_day(r)
    return rows


def seed_timeline(store, nights=14):
    """Identical nights → a computable, perfect SRI."""
    end = date.fromisoformat(TODAY)
    for i in range(nights):
        wake = end - timedelta(days=i)
        start = f"{(wake - timedelta(days=1)).isoformat()}T23:00:00"
        store.upsert_sleep_timeline(
            wake.isoformat(),
            json.dumps([["light", start, f"{wake.isoformat()}T07:00:00"]]),
        )


# --- shape and budget -------------------------------------------------------

def test_renders_within_cap_and_names_the_window(store):
    seed(store)
    out = run(FakeContext(store), days=28)
    assert "# Recovery — 28 days — Mon 2026-07-20" in out
    assert "VERDICT:" in out
    assert estimate_tokens(out) <= recovery.CAP


def test_wide_window_still_fits_the_cap(store):
    seed(store, nights=90)
    seed_timeline(store)
    out = run(FakeContext(store), days=90)
    assert estimate_tokens(out) <= recovery.CAP


def test_banner_is_carried_through(store):
    seed(store)
    out = run(FakeContext(store, banner="⚠ ACTIVE (since Thu 07-17): HRV below band"))
    assert out.startswith("⚠ ACTIVE")


def test_ensure_ready_is_called(store):
    seed(store)
    ctx = FakeContext(store)
    run(ctx)
    assert ctx.ready_calls == 1


# --- absent markers produce no row ------------------------------------------

def test_markers_the_device_never_produced_are_absent_not_null(store):
    """§3.2 principle: no fabricated values, and no 'null' rows either."""
    rows = make_days(TODAY, 40, resting_hr=46.0, daily_load=80.0)
    for r in rows:
        store.upsert_day(r)
    out = run(FakeContext(store))
    assert "Sleep debt" not in out
    assert "HRV (7d roll)" not in out
    assert "null" not in out.lower()
    assert "Resting HR" in out


def test_sleep_markers_appear_once_seeded(store):
    seed(store)
    seed_timeline(store)
    out = run(FakeContext(store))
    assert "Sleep debt" in out
    assert "Sleep regularity" in out


def test_empty_store_still_renders_a_valid_report(store):
    out = run(FakeContext(store))
    assert "# Recovery" in out and "VERDICT:" in out
    assert estimate_tokens(out) <= recovery.CAP


# --- the verdict is convergence's, and the athlete outranks sensors ---------

def test_hrv_row_prints_its_band_bounds(store):
    """E1 transparency: the HRV-vs-band row shows the band it compares against,
    so 'in band' is not a bare claim and cross-tool reads are legible."""
    rows = make_days(TODAY, 60, hrv_last_night=[70 if i % 2 else 95 for i in range(60)],
                     resting_hr=46.0, daily_load=80.0)
    for r in rows:
        store.upsert_day(r)
    out = run(FakeContext(store))
    line = next(line for line in out.splitlines() if "HRV (7d roll)" in line)
    assert re.search(r"\(band \d+–\d+\)", line)


def test_high_hrv_reads_above_band_but_does_not_flag_the_audit(store):
    """E1: recovery now agrees with brief that a high roll is 'above band', but
    high HRV is information — only a sustained drop feeds the audit (§3.2 #8)."""
    hrv = [80 if i % 2 else 90 for i in range(53)] + [140] * 7  # sharp recent rise
    rows = make_days(TODAY, 60, hrv_last_night=hrv, resting_hr=46.0, daily_load=80.0)
    for r in rows:
        store.upsert_day(r)
    out = run(FakeContext(store))
    line = next(line for line in out.splitlines() if "HRV (7d roll)" in line)
    assert "above band" in line
    assert line.rstrip().endswith("no |")  # deviation flag: not counted against the athlete


def test_calm_data_is_not_alarming(store):
    seed(store)
    seed_timeline(store)
    out = run(FakeContext(store))
    assert "0 of 3 marker groups deviant" in out


def test_sleep_debt_alone_is_one_group_not_an_alarm(store):
    seed(store, sleep_h=5.0, need_h=9.0)
    out = run(FakeContext(store))
    assert "1 of 3 marker groups deviant" in out


def test_logged_illness_today_caps_the_verdict_however_calm_the_sensors(store):
    """The project invariant, checked at the rendered surface."""
    seed(store)
    store.add_log({"date": TODAY, "flag": "illness", "note": "sore throat",
                   "created_at": TS})
    out = run(FakeContext(store))
    assert "illness logged today" in out


def test_unresolved_injury_is_surfaced(store):
    seed(store)
    store.add_log({"date": "2026-07-01", "flag": "injury", "note": "calf",
                   "resolved": 0, "created_at": TS})
    out = run(FakeContext(store))
    assert "unresolved injury" in out


# --- precedent line ---------------------------------------------------------

def test_no_precedent_line_without_episodes(store):
    seed(store)
    out = run(FakeContext(store))
    assert "Personal precedent" not in out


def test_precedent_line_appears_once_an_episode_exists(store):
    seed(store, nights=90)
    store.add_log({"date": "2026-06-01", "flag": "illness", "note": "overreached",
                   "created_at": TS})
    out = run(FakeContext(store))
    assert "Personal precedent" in out


def test_external_episodes_are_excluded_from_precedent_levels(store):
    """An episode noted EXTERNAL must not set load trigger levels — the
    salmonella case that produced false alarms on real data."""
    seed(store, nights=90)
    store.add_log({"date": "2026-06-01", "flag": "illness",
                   "note": "Salmonella. EXTERNAL cause — exclude from load levels.",
                   "created_at": TS})
    out = run(FakeContext(store))
    assert "Personal precedent" not in out


# --- parameter validation ---------------------------------------------------

@pytest.mark.parametrize("days", [6, 91, 0, -1])
def test_out_of_range_days_is_a_corrective_error(store, days):
    out = run(FakeContext(store), days=days)
    assert "days must be between 7 and 90" in out
    assert "garmin_recovery(days=28)" in out


def test_malformed_anchor_date_is_corrective(store):
    out = run(FakeContext(store), anchor_date="yesterday")
    assert "YYYY-MM-DD" in out
    assert TODAY in out


def test_anchor_date_moves_the_window(store):
    seed(store, nights=60)
    out = run(FakeContext(store), anchor_date="2026-07-10")
    assert "2026-07-10" in out


# --- breadcrumbs ------------------------------------------------------------

def test_breadcrumb_names_only_shipped_tools(store):
    seed(store)
    out = run(FakeContext(store))
    tail = out.rsplit("Next:", 1)[-1]
    for name in ("garmin_week", "garmin_load", "garmin_fitness",
                 "garmin_whats_changed", "garmin_reference"):
        assert name not in tail


def test_method_note_discloses_personal_baselines(store):
    seed(store)
    out = run(FakeContext(store))
    assert "own rolling baseline" in out


def test_percentile_ordinals_are_correct():
    """'83th' reads as a typo and undermines trust in the numbers beside it."""
    assert recovery._ordinal(83) == "83rd"
    assert recovery._ordinal(81) == "81st"
    assert recovery._ordinal(82) == "82nd"
    assert recovery._ordinal(84) == "84th"
    assert recovery._ordinal(11) == "11th"
    assert recovery._ordinal(12) == "12th"
    assert recovery._ordinal(13) == "13th"
    assert recovery._ordinal(100) == "100th"

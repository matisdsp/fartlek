"""Tests for the garmin_fitness tool.

Hermetic: seeded temp Store behind a FakeContext, no real ToolContext, no
network. The maths belongs to analytics/*; what is checked here is SELECTION
and PHRASING — above all that the race section branches on the KIND of goal,
because a Riegel time extrapolated to a 24h event and a Tanda marathon
regression are both fabrications on a fixed-time goal, and this tool is the
only place that distinction is enforced at the rendered surface.
"""
from __future__ import annotations

import asyncio

import pytest

from fartlek.mcp_server.tools import fitness
from fartlek.render.renderer import estimate_tokens

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
    return asyncio.run(fitness.run(ctx, **kw))


def seed_run(store, date, activity_id, *, n_laps=6, pace_s=300.0, hr=145,
             lap_m=1000.0, temp=15.0, vo2max=None, stopped_s=0.0):
    """One running activity plus its per-lap digest.

    Laps are uniform on purpose: a constant pace makes the band unambiguous,
    so a test that moves HR is moving exactly one variable.
    """
    hrs = hr if isinstance(hr, (list, tuple)) else [hr] * n_laps
    moving = n_laps * pace_s
    store.upsert_activity({
        "activity_id": activity_id, "date": date, "sport": "running",
        "name": "run", "duration_s": moving + stopped_s, "moving_s": moving,
        "distance_m": n_laps * lap_m, "avg_hr": int(hrs[0]), "load": 80.0,
        "vo2max": vo2max, "synced_at": TS,
    })
    store.replace_activity_laps(activity_id, [
        {"activity_id": activity_id, "lap_index": i, "distance_m": lap_m,
         "duration_s": pace_s + (stopped_s / n_laps), "moving_s": pace_s,
         "avg_hr": int(hrs[i]), "avg_speed": lap_m / pace_s, "temp_c": temp}
        for i in range(n_laps)
    ])


def seed_band(store, *, hrs=(150, 145, 140)):
    """Three months of identical-pace running with a falling HR."""
    for i, (month, hr) in enumerate(zip(("05", "06", "07"), hrs, strict=True)):
        for day in (5, 12, 19):
            seed_run(store, f"2026-{month}-{day:02d}", 1000 + i * 10 + day, hr=hr)


def seed_long_run(store, date="2026-06-14", activity_id=9001, n_laps=30,
                  stopped_s=0.0, hr=140):
    """A 2.5h, 30 km run — long enough to anchor a fixed-time projection."""
    seed_run(store, date, activity_id, n_laps=n_laps, hr=hr, stopped_s=stopped_s)


def set_goal(store, **kw):
    for key, value in kw.items():
        store.set_profile(key, str(value))


# --- shape and budget -------------------------------------------------------

def test_renders_within_cap_and_names_the_window(store):
    seed_band(store)
    out = run(FakeContext(store), weeks=12)
    assert "# Fitness & Race Outlook — 12 weeks — Mon 2026-07-20" in out
    assert "VERDICT:" in out
    assert estimate_tokens(out) <= fitness.CAP


def test_wide_window_with_a_goal_still_fits_the_cap(store):
    seed_band(store)
    seed_long_run(store, stopped_s=600.0)
    for i in range(40):
        seed_run(store, f"2026-0{4 + i % 3}-{1 + i % 28:02d}", 3000 + i,
                 hr=142, vo2max=58.0 + i * 0.05)
    set_goal(store, goal_distance="24h", goal_race_date="2026-08-29",
             goal_target_km=200)
    store.replace_pmc([{"date": TODAY, "load": 80.0, "ctl": 55.0,
                        "atl": 60.0, "tsb": -5.0}])
    out = run(FakeContext(store), weeks=52)
    assert estimate_tokens(out) <= fitness.CAP


def test_banner_is_carried_through(store):
    seed_band(store)
    out = run(FakeContext(store, banner="⚠ ACTIVE (since Thu 07-17): HRV below band"))
    assert out.startswith("⚠ ACTIVE")


def test_ensure_ready_is_called(store):
    ctx = FakeContext(store)
    run(ctx)
    assert ctx.ready_calls == 1


# --- fixed-time goal: a range, never a number -------------------------------

def test_fixed_time_goal_renders_a_distance_range_with_its_assumptions(store):
    """The project's reason for branching at all: 24h asks 'how far', and the
    honest answer is a band with the exponent and stoppage stated."""
    seed_band(store)
    seed_long_run(store, stopped_s=900.0)
    set_goal(store, goal_distance="24h", goal_race_date="2026-08-29")
    out = run(FakeContext(store))
    assert "projected distance" in out
    assert " km" in out and "–" in out.split("projected distance")[1][:40]
    assert "population default" in out          # exponent band disclosed
    assert "assumed stopped" in out             # stoppage assumption disclosed
    assert "confidence" in out


def test_fixed_time_goal_never_emits_a_riegel_race_time(store):
    """Riegel/Tanda over a 24h target would be fabrication (§3.2 amendment)."""
    seed_band(store)
    seed_long_run(store)
    set_goal(store, goal_distance="24h", goal_race_date="2026-08-29",
             pr_10k="38:43", pr_marathon="2:59:00")
    out = run(FakeContext(store))
    assert "Riegel" not in out
    assert "Tanda" not in out


def test_fixed_time_target_distance_is_compared_to_the_range(store):
    seed_band(store)
    seed_long_run(store)
    set_goal(store, goal_distance="24h", goal_target_km=200)
    out = run(FakeContext(store))
    assert "200 km" in out


def test_fixed_time_without_a_long_run_says_so_instead_of_projecting(store):
    seed_band(store)  # 30 min runs only — nothing to anchor 24h on
    set_goal(store, goal_distance="24h", goal_race_date="2026-08-29")
    out = run(FakeContext(store))
    assert "no long-run anchor" in out or "no run of ≥2h" in out
    assert "projected distance" not in out


def test_fixed_time_variants_are_all_recognised(store):
    seed_band(store)
    seed_long_run(store)
    for label in ("24h", "24 h", "12hr", "6 hours", "24-hour"):
        set_goal(store, goal_distance=label)
        out = run(FakeContext(store))
        assert "projected distance" in out, label


def test_declared_fixed_time_type_without_a_parsable_label(store):
    seed_band(store)
    seed_long_run(store)
    set_goal(store, goal_distance="ultra", goal_race_type="fixed_time",
             goal_hours=24)
    out = run(FakeContext(store))
    assert "projected distance" in out


# --- distance goal and no goal ----------------------------------------------

def test_distance_goal_without_prs_declines_to_predict(store):
    seed_band(store)
    set_goal(store, goal_distance="marathon", goal_race_date="2026-09-20")
    out = run(FakeContext(store))
    assert "no maximal performance" in out
    assert "Riegel" not in out


def test_distance_goal_with_prs_renders_riegel_only(store):
    seed_band(store)
    set_goal(store, goal_distance="marathon", goal_race_date="2026-09-20",
             goal_time="2:59:00", pr_10k="38:43", pr_half="1:25:00")
    out = run(FakeContext(store))
    assert "Riegel" in out
    assert "exponent" in out
    # Tanda may only appear as a disclosure of what is missing — never as a
    # second prediction, which would fake a consensus out of one model.
    assert "Tanda and the device prediction are not" in out
    assert "no consensus is claimed" in out


def test_distance_goal_uses_synced_personal_records(store):
    """PRs persisted at sync (not typed into the profile) drive Riegel — the
    distance-race branch is no longer dormant now that sync stores them."""
    seed_band(store)
    set_goal(store, goal_distance="marathon", goal_race_date="2026-09-20", goal_time="2:59:00")
    store.set_personal_records({
        "10k": {"seconds": 2323.0, "date": "2026-05-01", "activity_id": 1},   # 38:43
        "half": {"seconds": 5100.0, "date": "2026-04-10", "activity_id": 2},  # 1:25:00
    })
    out = run(FakeContext(store))
    assert "Riegel" in out and "exponent" in out


def test_no_goal_on_file_points_at_the_profile_tool(store):
    seed_band(store)
    out = run(FakeContext(store))
    assert "no goal race on file" in out
    assert "garmin_set_profile" in out


def test_goal_date_without_a_distance_is_incomplete_not_guessed(store):
    seed_band(store)
    set_goal(store, goal_race_date="2026-09-20")
    out = run(FakeContext(store))
    assert "without a distance" in out


# --- absent data omits sections, never null rows ----------------------------

def test_empty_store_still_renders_a_valid_report(store):
    out = run(FakeContext(store))
    assert "# Fitness & Race Outlook" in out and "VERDICT:" in out
    assert "no fitness outcomes trackable" in out
    assert "None" not in out
    assert estimate_tokens(out) <= fitness.CAP


def test_absent_vo2max_omits_the_row(store):
    seed_band(store)
    out = run(FakeContext(store))
    assert "VO2max" not in out
    assert "HR at" in out


def test_vo2max_row_appears_once_the_device_produced_it(store):
    seed_band(store)
    seed_run(store, "2026-07-19", 7777, vo2max=61.0)
    out = run(FakeContext(store))
    assert "VO2max" in out and "61.0" in out


def test_no_laps_means_no_efficiency_row(store):
    store.upsert_activity({"activity_id": 1, "date": "2026-07-01",
                           "sport": "running", "duration_s": 3600,
                           "moving_s": 3600, "distance_m": 12000,
                           "load": 80.0, "synced_at": TS})
    out = run(FakeContext(store))
    assert "HR at" not in out
    assert "None" not in out


def test_durability_row_only_once_a_long_run_exists(store):
    seed_band(store)
    assert "Decoupling" not in run(FakeContext(store))
    seed_long_run(store, hr=[135 + i // 6 for i in range(30)])
    out = run(FakeContext(store))
    assert "Decoupling" in out
    assert "LOW confidence" in out


def test_hr_at_pace_move_drives_the_verdict(store):
    seed_band(store, hrs=(150, 145, 138))
    out = run(FakeContext(store))
    assert "aerobic fitness rising" in out
    assert "→" in out


def test_rising_hr_at_the_same_pace_is_reported_as_such(store):
    seed_band(store, hrs=(138, 145, 150))
    out = run(FakeContext(store))
    assert "drifted up" in out


# --- forward projection -----------------------------------------------------

def test_projection_renders_with_a_future_goal_and_a_pmc(store):
    seed_band(store)
    store.upsert_day({"date": TODAY, "daily_load": 80.0, "synced_at": TS})
    store.replace_pmc([{"date": TODAY, "load": 80.0, "ctl": 55.0,
                        "atl": 60.0, "tsb": -5.0}])
    set_goal(store, goal_distance="marathon", goal_race_date="2026-08-29")
    out = run(FakeContext(store))
    assert "Projection" in out and "CTL" in out
    assert "basis:" in out


def test_no_pmc_means_no_projection_line(store):
    seed_band(store)
    set_goal(store, goal_distance="marathon", goal_race_date="2026-08-29")
    assert "Projection" not in run(FakeContext(store))


def test_past_goal_date_does_not_project(store):
    seed_band(store)
    store.replace_pmc([{"date": TODAY, "load": 80.0, "ctl": 55.0,
                        "atl": 60.0, "tsb": -5.0}])
    set_goal(store, goal_distance="marathon", goal_race_date="2026-01-01")
    assert "Projection" not in run(FakeContext(store))


# --- parameter validation ---------------------------------------------------

@pytest.mark.parametrize("weeks", [3, 53, 0, -1])
def test_out_of_range_weeks_is_a_corrective_error(store, weeks):
    out = run(FakeContext(store), weeks=weeks)
    assert "weeks must be between 4 and 52" in out
    assert TODAY in out
    assert "garmin_fitness(weeks=12)" in out


def test_malformed_anchor_date_is_corrective(store):
    out = run(FakeContext(store), anchor_date="last summer")
    assert "YYYY-MM-DD" in out
    assert TODAY in out
    assert "garmin_fitness(weeks=12)" in out


def test_anchor_date_moves_the_window(store):
    seed_band(store)
    out = run(FakeContext(store), anchor_date="2026-06-30")
    assert "2026-06-30" in out


def test_malformed_stored_goal_date_does_not_crash(store):
    seed_band(store)
    set_goal(store, goal_distance="marathon", goal_race_date="soon")
    out = run(FakeContext(store), weeks=12)
    assert "VERDICT:" in out


# --- breadcrumbs ------------------------------------------------------------

def test_breadcrumb_names_only_shipped_tools(store):
    seed_band(store)
    out = run(FakeContext(store))
    for name in ("garmin_week", "garmin_load", "garmin_whats_changed",
                 "garmin_reference", "garmin_apply_plan"):
        assert name not in out


def test_method_note_discloses_the_primary_measure_and_the_heat_guard(store):
    seed_band(store)
    out = run(FakeContext(store))
    assert "primary efficiency measure" in out
    assert "24 °C" in out


# --- pure helpers -----------------------------------------------------------

def test_goal_kind_classification():
    assert fitness._goal({"goal_distance": "24h"})["kind"] == "fixed_time"
    assert fitness._goal({"goal_distance": "24h"})["hours"] == 24
    assert fitness._goal({"goal_distance": "marathon"})["kind"] == "distance"
    assert fitness._goal({"goal_distance": "half"})["distance_m"] == 21097.5
    assert fitness._goal({})["kind"] == "none"
    custom = fitness._goal({"goal_distance": "custom", "goal_custom_km": "50"})
    assert custom["kind"] == "distance" and custom["distance_m"] == 50000.0


def test_hms_parsing_accepts_both_pr_and_goal_forms():
    assert fitness._parse_hms("2:59:00") == 10740.0
    assert fitness._parse_hms("38:43") == 2323.0
    assert fitness._parse_hms("nonsense") is None
    assert fitness._parse_hms(None) is None

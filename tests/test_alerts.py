"""Tests for fartlek.analytics.alerts (anomaly scan + resolution)."""
from __future__ import annotations

from datetime import date, timedelta

from conftest import make_series

from fartlek.analytics.alerts import (
    _baseline90,
    resolution_dates,
    scan,
    tolerance_alert,
    tracked_metrics,
)

END = "2026-07-19"

# median 50, MAD 1 → mad_sd 1.4826; z(52)=1.35, z(55)=3.37 (robust to a few outliers)
BASE = [49.0, 50.0, 51.0] * 10
SLEEP_BASE = [79.0, 80.0, 81.0] * 10  # median 80, mad_sd 1.4826
HRV_BASE = [79.0, 80.0, 81.0] * 10    # median 80, mad_sd 1.4826 (floored at 2.0)
LOAD_BASE = [90.0, 100.0, 110.0] * 10  # median 100, mad_sd 14.826


def days_before(end: str, n: int) -> str:
    return (date.fromisoformat(end) - timedelta(days=n)).isoformat()


def test_tracked_metrics():
    assert tracked_metrics() == [
        "resting_hr",
        "hrv_last_night",
        "sleep_score",
        "sleep_duration_h",
        "body_battery_wake",
        "avg_stress",
        "daily_load",
    ]


# --- scan: trip rules -------------------------------------------------------

def test_quiet_series_no_alerts():
    assert scan({"resting_hr": make_series(END, BASE)}, END) == []


def test_single_day_spike_watch_and_message_format():
    alerts = scan({"resting_hr": make_series(END, BASE + [55.0])}, END)
    assert alerts == [
        {
            "metric": "resting_hr",
            "severity": "WATCH",
            "message": "resting_hr high — 55 vs 50 (90d), 1d streak",
            "since_date": END,
        }
    ]


def test_out_of_band_streak_trips_without_z2():
    # z(52) ≈ 1.35: out of band but never |z|>2 — trips via the 3-day streak.
    alerts = scan({"resting_hr": make_series(END, BASE + [52.0, 52.0, 52.0])}, END)
    assert len(alerts) == 1
    assert alerts[0]["severity"] == "WATCH"
    assert alerts[0]["since_date"] == days_before(END, 2)
    assert alerts[0]["message"] == "resting_hr high — 52 vs 50 (90d), 3d streak"


def test_two_out_of_band_days_do_not_trip():
    assert scan({"resting_hr": make_series(END, BASE + [52.0, 52.0])}, END) == []


def test_low_direction_in_message():
    """HRV has no minimum-streak rule, so one severe night alerts on its own."""
    alerts = scan({"hrv_last_night": make_series(END, HRV_BASE + [50.0])}, END)
    assert alerts[0]["message"] == "hrv_last_night low — 50 vs 80 (90d), 1d streak"


def test_one_alert_per_metric_when_both_rules_trip():
    alerts = scan({"resting_hr": make_series(END, BASE + [55.0, 55.0, 55.0])}, END)
    assert len(alerts) == 1


def test_end_date_excludes_later_points():
    series = make_series(END, BASE + [55.0])  # spike lands on END
    assert scan({"resting_hr": series}, days_before(END, 1)) == []


def test_missing_or_empty_series_ignored():
    assert scan({}, END) == []
    assert scan({"resting_hr": []}, END) == []
    assert scan({"not_tracked": make_series(END, BASE + [55.0])}, END) == []


# --- scan: severity ---------------------------------------------------------

def test_hard_streak_3d_escalates_to_amber():
    alerts = scan({"resting_hr": make_series(END, BASE + [55.0, 55.0, 55.0])}, END)
    assert alerts[0]["severity"] == "AMBER"
    assert alerts[0]["since_date"] == days_before(END, 2)
    assert alerts[0]["message"] == "resting_hr high — 55 vs 50 (90d), 3d streak"


def test_hard_streak_2d_stays_watch():
    alerts = scan({"resting_hr": make_series(END, BASE + [55.0, 55.0])}, END)
    assert alerts[0]["severity"] == "WATCH"


def test_two_metrics_tripping_same_day_escalate_to_amber():
    alerts = scan(
        {
            "resting_hr": make_series(END, BASE + [55.0]),
            "hrv_last_night": make_series(END, HRV_BASE + [50.0]),
        },
        END,
    )
    assert {a["metric"] for a in alerts} == {"resting_hr", "hrv_last_night"}
    assert all(a["severity"] == "AMBER" for a in alerts)
    assert all(a["since_date"] == END for a in alerts)


def test_two_metrics_different_since_dates_stay_watch():
    alerts = scan(
        {
            "resting_hr": make_series(END, BASE + [55.0]),  # since END
            # mad_sd floors at 2.0 (resolution floor) → z(76) = -2.0:
            # 4-day out-of-band streak, since END-3, never severe (needs >2)
            "sleep_score": make_series(END, SLEEP_BASE + [76.0, 76.0, 76.0, 76.0]),
        },
        END,
    )
    assert len(alerts) == 2
    assert all(a["severity"] == "WATCH" for a in alerts)


def test_phase0_never_emits_red():
    alerts = scan(
        {
            "resting_hr": make_series(END, BASE + [70.0] * 10),
            "sleep_score": make_series(END, SLEEP_BASE + [40.0] * 10),
            "daily_load": make_series(END, LOAD_BASE + [400.0] * 10),
        },
        END,
    )
    assert alerts and all(a["severity"] in ("WATCH", "AMBER") for a in alerts)


# --- scan: daily_load high side only ----------------------------------------

def test_daily_load_spike_alerts():
    alerts = scan({"daily_load": make_series(END, LOAD_BASE + [170.0])}, END)
    assert alerts == [
        {
            "metric": "daily_load",
            "severity": "WATCH",
            "message": "daily_load high — 170 vs 100 (90d), 1d streak",
            "since_date": END,
        }
    ]


def test_daily_load_collapse_never_alerts():
    # z(30) strongly negative — low side never trips daily_load.
    assert scan({"daily_load": make_series(END, LOAD_BASE + [30.0])}, END) == []
    # nor does a low-side streak (z(70) ≈ -2.0 for 3 days)
    assert scan({"daily_load": make_series(END, LOAD_BASE + [70.0, 70.0, 70.0])}, END) == []


# --- resolution_dates -------------------------------------------------------

def test_not_resolved_after_one_in_band_day():
    series = make_series(END, BASE + [55.0, 55.0, 55.0, 50.0])
    assert resolution_dates({"resting_hr": series}, ["resting_hr"], END) == {}


def test_resolved_after_exactly_two_in_band_days():
    series = make_series(END, BASE + [55.0, 55.0, 55.0, 50.0, 50.0])
    assert resolution_dates({"resting_hr": series}, ["resting_hr"], END) == {"resting_hr": END}


def test_resolution_only_for_active_metrics():
    series = make_series(END, BASE)  # fully in band
    assert resolution_dates({"resting_hr": series}, [], END) == {}
    assert resolution_dates({"resting_hr": series}, ["sleep_score"], END) == {}


def test_resolution_needs_two_points():
    series = make_series(END, [50.0])
    assert resolution_dates({"resting_hr": series}, ["resting_hr"], END) == {}


def test_favourable_deviation_resolves_an_alert():
    """An RHR that overshoots DOWNWARD is an improvement, so it clears a
    high-side alert instead of holding it open. Before the 2026-07-22 tuning
    this counted as still-out-of-band and kept the banner up."""
    series = make_series(END, BASE + [55.0, 55.0, 55.0, 40.0, 40.0])
    assert resolution_dates({"resting_hr": series}, ["resting_hr"], END) == {"resting_hr": END}


def test_daily_load_resolves_on_rest_days():
    # Band is one-sided for daily_load: rest days (very negative z) resolve a spike.
    series = make_series(END, LOAD_BASE + [170.0, 170.0, 170.0, 20.0, 20.0])
    assert resolution_dates({"daily_load": series}, ["daily_load"], END) == {"daily_load": END}


def test_resolution_respects_end_date():
    # As of the first in-band day, not yet resolved; one day later, resolved.
    series = make_series(END, BASE + [55.0, 55.0, 55.0, 50.0, 50.0])
    assert resolution_dates({"resting_hr": series}, ["resting_hr"], days_before(END, 1)) == {}


# --- tuning against the maintainer's real history (2026-07-22) --------------
#
# Replaying the scanner over 116 real days produced 75 alerts — one every 1.5
# days, of which 27 were AMBER (a banner on EVERY tool response). A scanner
# that noisy is ignored within a fortnight, which is the trust failure this
# project exists to avoid. Three rules were agreed with the athlete; these
# tests hold them.

def test_favourable_deviation_never_alerts():
    """31% of the original alerts fired on an IMPROVEMENT — 'resting_hr low —
    43 vs 47', 'hrv high — 115 vs 86'. Interrupting someone to report good
    news spends the attention budget that real warnings need."""
    assert scan({"resting_hr": make_series(END, BASE + [40.0, 40.0, 40.0])}, END) == []
    assert scan({"hrv_last_night": make_series(END, HRV_BASE + [110.0, 110.0, 110.0])}, END) == []
    assert scan({"avg_stress": make_series(END, SLEEP_BASE + [60.0, 60.0, 60.0])}, END) == []
    assert scan({"sleep_duration_h": make_series(END, [7.0] * 30 + [9.5, 9.5, 9.5])}, END) == []


def test_adverse_deviation_still_alerts():
    """The other side must keep working — this is a filter, not a mute."""
    assert scan({"resting_hr": make_series(END, BASE + [55.0])}, END)
    assert scan({"hrv_last_night": make_series(END, HRV_BASE + [50.0])}, END)


def test_load_baseline_ignores_rest_days():
    """daily_load's median included rest days, so on the real account it sat
    at 68 while a normal weekly long run scored 375 — every long run tripped
    by construction. Sessions are now compared to sessions."""
    # 4 rest days and 3 training days per week: the all-day median is a rest
    # day, the training-day median is a session.
    pts = make_series(END, [0.0, 0.0, 0.0, 0.0, 100.0, 120.0, 80.0] * 13)

    med_load, _sd, n_load = _baseline90(pts, END, "daily_load")
    med_plain, _sd2, n_plain = _baseline90(pts, END, "resting_hr")

    assert med_load == 100.0, "training-day median"
    assert med_plain == 0.0, "all-day median is a rest day — the old behaviour"
    assert n_load < n_plain


def test_normal_session_does_not_alert_but_an_extreme_one_does():
    week = [0.0, 0.0, 60.0, 100.0, 140.0, 180.0, 80.0]
    assert scan({"daily_load": make_series(END, week * 13 + [150.0])}, END) == []
    assert scan({"daily_load": make_series(END, week * 13 + [600.0])}, END)


def test_isolated_short_night_does_not_alert_but_two_in_a_row_do():
    """This athlete sleeps 6.2h against an 8.9h need: isolated short nights are
    normal and they already know. Two consecutive is the signal they may have
    missed."""
    base = [7.0] * 40
    assert scan({"sleep_duration_h": make_series(END, base + [2.5])}, END) == []
    assert scan({"sleep_duration_h": make_series(END, base + [2.5, 2.5])}, END)


def test_salmonella_episode_is_still_detected():
    """Non-regression on a CERTIFIED positive: the athlete had salmonella from
    Sun 2026-04-19 to Wed 2026-04-22, and on 04-20 five markers deviated at
    once (HRV 38 vs 81, sleep 2.1h, sleep score 5, stress 46, RHR high).

    No amount of noise-reduction may silence this. It is the one day in six
    months of history we know for certain the system SHOULD shout about.
    """
    end = "2026-04-20"
    hrv = make_series(end, [80.0] * 40 + [38.0])
    stress = make_series(end, [30.0] * 40 + [46.0])
    rhr = make_series(end, BASE + [53.0, 53.0])
    sleep_h = make_series(end, [7.0] * 40 + [3.0, 2.1])

    alerts = scan({"hrv_last_night": hrv, "avg_stress": stress,
                   "resting_hr": rhr, "sleep_duration_h": sleep_h}, end)
    fired = {a["metric"] for a in alerts}

    assert "hrv_last_night" in fired, "a collapsed HRV during illness must alert"
    assert "sleep_duration_h" in fired, "two short nights in a row must alert"
    assert len(fired) >= 3, f"multi-marker illness day under-detected: {fired}"
    # Several markers deviating the same day is exactly the escalation case.
    assert any(a["severity"] == "AMBER" for a in alerts)


def test_tolerance_alert_fires_only_over_capacity():
    assert tolerance_alert(None, "2026-07-20") is None   # no data → no alarm
    assert tolerance_alert(0.9, "2026-07-20") is None    # under capacity
    assert tolerance_alert(1.0, "2026-07-20") is None    # exactly at capacity
    al = tolerance_alert(1.3, "2026-07-20")
    assert al == {"metric": "running_tolerance", "severity": "WATCH",
                  "message": "running tolerance over capacity — impact load 130% of tolerance",
                  "since_date": "2026-07-20"}

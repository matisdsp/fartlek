"""Tests for fartlek.analytics.alerts (anomaly scan + resolution)."""
from __future__ import annotations

from datetime import date, timedelta

from conftest import make_series

from fartlek.analytics.alerts import resolution_dates, scan, tracked_metrics

END = "2026-07-19"

# median 50, MAD 1 → mad_sd 1.4826; z(52)=1.35, z(55)=3.37 (robust to a few outliers)
BASE = [49.0, 50.0, 51.0] * 10
SLEEP_BASE = [79.0, 80.0, 81.0] * 10  # median 80, mad_sd 1.4826
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
    alerts = scan({"sleep_score": make_series(END, SLEEP_BASE + [74.0])}, END)
    assert alerts[0]["message"] == "sleep_score low — 74 vs 80 (90d), 1d streak"


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
            "sleep_score": make_series(END, SLEEP_BASE + [74.0]),
        },
        END,
    )
    assert {a["metric"] for a in alerts} == {"resting_hr", "sleep_score"}
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


def test_out_of_band_low_blocks_resolution_for_two_sided_metric():
    series = make_series(END, BASE + [55.0, 55.0, 55.0, 40.0, 40.0])  # z(40) « -1
    assert resolution_dates({"resting_hr": series}, ["resting_hr"], END) == {}


def test_daily_load_resolves_on_rest_days():
    # Band is one-sided for daily_load: rest days (very negative z) resolve a spike.
    series = make_series(END, LOAD_BASE + [170.0, 170.0, 170.0, 20.0, 20.0])
    assert resolution_dates({"daily_load": series}, ["daily_load"], END) == {"daily_load": END}


def test_resolution_respects_end_date():
    # As of the first in-band day, not yet resolved; one day later, resolved.
    series = make_series(END, BASE + [55.0, 55.0, 55.0, 50.0, 50.0])
    assert resolution_dates({"resting_hr": series}, ["resting_hr"], days_before(END, 1)) == {}

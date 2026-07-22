"""Training intensity distribution (DESIGN.md §3.2 #11).

The contract is that the athlete is judged against their OWN norm, that the
zone approximation is disclosed rather than hidden, and that the one
universally unwelcome pattern — grey-zone creep — is detected.
"""
from __future__ import annotations

import pytest

from fartlek.analytics import tid

# The maintainer's real Garmin configuration.
FLOORS = [101.0, 121.0, 142.0, 164.0, 183.0]
MAX_HR = 195.0
LT2 = 183.0
LT1 = 160.0

CONF = {"zone_floors": FLOORS, "max_hr": MAX_HR, "lt1": LT1, "lt2": LT2}


def act(date="2026-07-01", z=(600.0, 1200.0, 300.0, 0.0, 0.0)):
    return {"date": date, **{f"hr_z{i + 1}_s": v for i, v in enumerate(z)}}


# --- zone mapping -----------------------------------------------------------

def test_prorates_a_zone_that_straddles_a_threshold():
    """Zone 3 spans 142-164 and LT1 is 160, so 18/22 of its time is easy and
    4/22 moderate. Whole-bucket containment would call all of it moderate."""
    res = tid.map_to_three_zones([0, 0, 2200.0, 0, 0], **CONF)
    assert res["method"] == "prorated"
    assert res["easy"] == pytest.approx(2200.0 * 18 / 22)
    assert res["moderate"] == pytest.approx(2200.0 * 4 / 22)
    assert res["hard"] == 0.0


def test_zones_entirely_below_lt1_are_all_easy():
    res = tid.map_to_three_zones([600.0, 1200.0, 0, 0, 0], **CONF)
    assert res["easy"] == pytest.approx(1800.0)
    assert res["moderate"] == 0.0 and res["hard"] == 0.0


def test_zone_above_lt2_is_all_hard():
    res = tid.map_to_three_zones([0, 0, 0, 0, 900.0], **CONF)
    assert res["hard"] == pytest.approx(900.0)


def test_falls_back_to_bucket_containment_without_boundaries():
    """Approximation is allowed; hiding it is not."""
    res = tid.map_to_three_zones([600.0, 1200.0, 300.0, 120.0, 60.0])
    assert res["method"] == "buckets_approximate"
    assert res["easy"] == 1800.0 and res["moderate"] == 300.0 and res["hard"] == 180.0


def test_no_data_is_distinguished_from_all_easy():
    res = tid.map_to_three_zones([0, 0, 0, 0, 0], **CONF)
    assert res["method"] == "no_data"
    assert tid.shares(res) is None


def test_shares_sum_to_one():
    sh = tid.shares(tid.map_to_three_zones([600.0, 1200.0, 300.0, 120.0, 60.0], **CONF))
    assert sum(sh) == pytest.approx(1.0)


def test_lt1_estimate_matches_the_published_formula():
    assert tid.lt1_estimate(44.0, 198.0) == pytest.approx(44 + 0.75 * 154)


# --- aggregation ------------------------------------------------------------

def test_long_sessions_are_weighted_by_their_duration():
    """Summing seconds before mapping keeps a 3-hour run from counting the
    same as a 20-minute one."""
    long_easy = act(z=(0.0, 10800.0, 0.0, 0.0, 0.0))
    short_hard = act(z=(0.0, 0.0, 0.0, 0.0, 600.0))
    sh = tid.shares(tid.distribution([long_easy, short_hard], **CONF))
    assert sh[0] > 0.9 and sh[2] < 0.1


def test_activities_without_zone_data_are_skipped():
    res = tid.distribution([act(), {"date": "2026-07-02"}], **CONF)
    assert res["n_activities"] == 1


# --- classification ---------------------------------------------------------

@pytest.mark.parametrize(
    "share, expected",
    [
        ((0.95, 0.04, 0.01), "base"),
        ((0.80, 0.05, 0.15), "polarized"),
        ((0.70, 0.20, 0.10), "pyramidal"),
        ((0.50, 0.35, 0.15), "threshold"),
    ],
)
def test_classification(share, expected):
    assert tid.classify(share) == expected


def test_base_block_is_not_a_fault():
    """An ultra athlete running 95% easy is following their plan. The
    classifier must have a name for that instead of calling it broken."""
    assert tid.classify((0.96, 0.03, 0.01)) == "base"


def test_unknown_without_data():
    assert tid.classify(None) == "unknown"


# --- grey-zone creep --------------------------------------------------------

def _weeks(values):
    return [(f"2026-W{20 + i:02d}", v) for i, v in enumerate(values)]


def test_creep_detected_after_three_rising_weeks():
    res = tid.grey_zone_creep(_weeks([0.08, 0.10, 0.14, 0.19]))
    assert res["creeping"] is True
    assert res["weeks"] == 3
    assert res["rise"] == pytest.approx(0.11)


def test_two_rising_weeks_is_not_creep():
    assert tid.grey_zone_creep(_weeks([0.08, 0.09, 0.12]))["creeping"] is False


def test_a_trivial_rise_is_not_creep():
    """Three consecutive rises of a fraction of a point are noise."""
    res = tid.grey_zone_creep(_weeks([0.100, 0.101, 0.102, 0.103]))
    assert res["weeks"] == 3 and res["creeping"] is False


def test_a_recent_drop_resets_the_run():
    assert tid.grey_zone_creep(_weeks([0.08, 0.12, 0.16, 0.09]))["creeping"] is False


def test_creep_needs_enough_weeks():
    res = tid.grey_zone_creep(_weeks([0.10, 0.12]))
    assert res["creeping"] is False and "need" in res["reason"]


def test_weekly_mid_shares_buckets_by_iso_week():
    acts = [act("2026-05-04"), act("2026-05-05"), act("2026-06-15")]
    weeks = tid.weekly_mid_shares(acts, **CONF)
    assert [w for w, _ in weeks] == ["2026-W19", "2026-W25"]


# --- drift vs the athlete's own norm ---------------------------------------

def test_drift_is_measured_against_the_athletes_own_norm():
    norm = (0.80, 0.10, 0.10)
    assert tid.drift_vs_norm((0.79, 0.11, 0.10), norm)["drifted"] is False
    drifted = tid.drift_vs_norm((0.65, 0.25, 0.10), norm)
    assert drifted["drifted"] is True
    assert drifted["deltas"][1] == pytest.approx(0.15)


def test_no_drift_verdict_without_a_norm():
    res = tid.drift_vs_norm((0.8, 0.1, 0.1), None)
    assert res["drifted"] is False and res["reason"] == "insufficient data"

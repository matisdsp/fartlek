"""Race models (DESIGN.md §3.2 #16 + fixed-time amendment).

The contract: fixed-time events get their own model, exponents are population
defaults unless fitted on genuine maximal performances, and every projection
carries its assumptions so a coach can disagree with the inputs rather than
the output.
"""
from __future__ import annotations

import pytest

from fartlek.analytics import race

HOUR = 3600.0


# --- Riegel both ways -------------------------------------------------------

def test_riegel_time_matches_the_published_formula():
    """40:00 for 10K at b=1.06 -> the textbook half-marathon prediction."""
    t = race.riegel_time(2400.0, 10_000.0, 21_097.5, b=1.06)
    assert t == pytest.approx(2400.0 * (21_097.5 / 10_000.0) ** 1.06)
    assert 5200 < t < 5400          # ~1h28, the known answer


def test_distance_and_time_forms_are_exact_inverses():
    d2 = race.riegel_distance(10_000.0, 2400.0, 5300.0, b=1.09)
    assert race.riegel_time(2400.0, 10_000.0, d2, b=1.09) == pytest.approx(5300.0)


def test_distance_grows_sub_linearly_with_time():
    """Tripling the duration must not triple the distance — the entire point
    of the exponent."""
    d1, t1 = 69_100.0, 7.62 * HOUR
    tripled = race.riegel_distance(d1, t1, 3 * t1, b=1.06)
    assert tripled < 3 * d1
    assert tripled > 2 * d1         # but it is not flat either


def test_harsher_exponent_gives_less_distance():
    args = (69_100.0, 7.62 * HOUR, 24 * HOUR)
    assert race.riegel_distance(*args, b=1.15) < race.riegel_distance(*args, b=1.06)


def test_non_positive_inputs_are_rejected():
    with pytest.raises(ValueError):
        race.riegel_distance(0.0, 100.0, 100.0)
    with pytest.raises(ValueError):
        race.riegel_time(100.0, 100.0, 0.0)


# --- exponent fitting -------------------------------------------------------

def test_fit_recovers_a_known_exponent_from_maximal_performances():
    b_true = 1.08
    perfs = [(d, race.riegel_time(2400.0, 10_000.0, d, b=b_true))
             for d in (5_000.0, 10_000.0, 21_097.5, 42_195.0)]
    fit = race.fit_riegel_exponent(perfs)
    assert fit["b"] == pytest.approx(b_true, abs=1e-6)
    assert fit["quality"] == "good" and fit["clamped"] is False


def test_fit_falls_back_to_the_default_below_two_points():
    fit = race.fit_riegel_exponent([(10_000.0, 2400.0)])
    assert fit["b"] == race.RIEGEL_DEFAULT_B and fit["quality"] == "default"


def test_implausible_fit_is_clamped_and_says_so():
    """Sub-maximal training runs can imply b < 1 — better than linear, which
    is impossible. The clamp must be visible, not silent."""
    perfs = [(30_000.0, 3.5 * HOUR), (69_000.0, 7.6 * HOUR)]
    fit = race.fit_riegel_exponent(perfs)
    assert fit["raw_b"] < 1.0
    assert fit["b"] == race.RIEGEL_BOUNDS[0]
    assert fit["clamped"] is True


# --- stoppage ---------------------------------------------------------------

def test_stoppage_ratio_from_laps():
    laps = [{"duration_s": 600.0, "moving_s": 570.0},
            {"duration_s": 600.0, "moving_s": 600.0}]
    assert race.stoppage_ratio(laps) == pytest.approx(30.0 / 1200.0)


def test_stoppage_is_none_without_data():
    assert race.stoppage_ratio([]) is None


def test_missing_moving_time_assumes_continuous_movement():
    assert race.stoppage_ratio([{"duration_s": 600.0}]) == pytest.approx(0.0)


# --- fixed-time projection --------------------------------------------------

def _project(**kw):
    base = dict(reference_distance_m=69_100.0, reference_moving_s=7.33 * HOUR,
                target_hours=24.0)
    base.update(kw)
    return race.fixed_time_projection(**base)


def test_projection_returns_a_range_not_a_number():
    """A single figure would imply a precision the model does not have."""
    res = _project()
    assert res["low_m"] < res["mid_m"] < res["high_m"]
    assert res["band"] == race.FIXED_TIME_EXPONENT_BAND


def test_stoppage_reduces_distance_and_is_reported_separately():
    """Time at the aid table is the one variable an athlete fully controls, so
    it is modelled explicitly rather than folded into pace."""
    moving = _project(stoppage=0.0)
    stopping = _project(stoppage=0.10)
    assert stopping["high_m"] < moving["high_m"]
    assert stopping["moving_hours"] == pytest.approx(21.6)
    assert any("stopped" in a for a in stopping["assumptions"])


def test_submaximal_reference_forces_low_confidence():
    assert _project(reference_was_maximal=False)["confidence"] == "low"
    assert any("sub-maximal" in a for a in _project()["assumptions"])


def test_long_extrapolation_is_declared():
    res = _project(reference_moving_s=3.0 * HOUR, reference_distance_m=30_000.0)
    assert res["extrapolation_ratio"] > race.MAX_EXTRAPOLATION_RATIO
    assert any("extrapolating" in a for a in res["assumptions"])
    assert res["confidence"] == "low"


def test_population_exponent_is_always_labelled_as_such():
    """Principle: a threshold must never look personally derived when it is a
    population default."""
    assert any("population default" in a for a in _project()["assumptions"])


def test_too_short_a_reference_is_refused_not_extrapolated():
    res = _project(reference_moving_s=1.0 * HOUR, reference_distance_m=12_000.0)
    assert "error" in res and "too short" in res["error"]


def test_reference_must_be_positive():
    with pytest.raises(ValueError):
        _project(reference_distance_m=0.0)


# --- field comparison -------------------------------------------------------

FIELD_2025 = [225_684, 215_808, 202_287, 201_777, 183_309, 164_382, 155_541]


def test_field_comparison_places_a_projection():
    """200 km is a podium on one course and mid-pack on another; the number
    means nothing without the field."""
    res = race.compare_to_field(200_000.0, FIELD_2025)
    assert res["rank"] == 5        # four finishers beat it
    assert res["n"] == 7
    assert res["percentile"] == pytest.approx(100.0 * (1 - 4 / 7))


def test_a_winning_projection_ranks_first():
    assert race.compare_to_field(230_000.0, FIELD_2025)["rank"] == 1


def test_empty_field_is_not_an_error():
    res = race.compare_to_field(200_000.0, [])
    assert res["rank"] is None and res["n"] == 0

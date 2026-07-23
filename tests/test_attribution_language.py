"""Attribution-language gate (DESIGN §4.5, invariant §8.7).

The server may make a causal claim in exactly the five situations
`analytics.attribution` sanctions; everything else is co-occurrence ("X while
Y"). This gate keeps that closed set closed on three surfaces:

1. the engine — every causal `statement` it can emit is one of the sanctioned
   phrasings, and none smuggles in a free "because";
2. the glossary — `garmin_reference` documents exactly these five rules, and
   interpolates the engine's own constants so the two cannot drift;
3. the render surface — no shipped render says "because" outside the glossary
   that explains the rule. Attribution is not wired into a synthesis tool yet
   (the glossary says so), so this is future-proofing: the day a tool renders a
   cause, an unsanctioned one fails the build instead of reaching an athlete.

Note on "matches": the planned-vs-executed matcher (§3.2 #15) legitimately says
a session "matches" a plan — a compliance statement, not a cause — so the
render scan targets the literal "because", which the sanctioned causal
phrasings never use ("matches …, not …" / "suppressed by …, not …" / "tends to
be followed by …"). The engine test below covers the "matches" phrasings.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

import pytest

from fartlek.analytics import attribution as A
from fartlek.mcp_server.tools import reference
from tests.conftest import make_series
from tests.golden_renders import GOLDENS


def _all_engine_statements() -> dict[str, str]:
    """One triggering call per rule → {rule_id: statement}. Perfectly
    correlated series drive the lagged rules past their guards."""
    stmts: dict[str, str] = {}

    a = A.deep_sleep_attribution(
        deep_sleep_declining=True, bedtime_sd_h=1.5, ramp_pct=2.0, strain_pctile=50.0)
    b = A.deep_sleep_attribution(
        deep_sleep_declining=True, bedtime_sd_h=0.3, ramp_pct=12.0, strain_pctile=50.0)
    e = A.heat_ef_attribution(
        ef_declining=True, hot_share=0.7, cool_ef=2.0, hot_ef=1.8)
    for r in (a, b, e):
        assert r is not None
        stmts[r["rule"]] = r["statement"]

    # lagged load→HRV and debt→HRV: response on day D+1 tracks driver on day D
    driver = make_series("2026-07-20", [float(i % 13) for i in range(80)])
    response = [
        ((date.fromisoformat(d) + timedelta(days=1)).isoformat(), v)
        for d, v in driver
    ]
    for rule, label in (("load_hrv_lag", "load"), ("debt_hrv_lag", "sleep debt")):
        r = A.lagged_association(driver, response, rule=rule, label=label)
        assert r is not None, f"{rule} should trigger on a perfectly correlated series"
        stmts[rule] = r["statement"]

    return stmts


def test_rule_ids_are_the_closed_five():
    assert A.RULE_IDS == ("late_bedtimes", "load_driven",
                          "load_hrv_lag", "debt_hrv_lag", "heat_ef")
    # co-occurrence is the only fallback phrasing, and it is not causal
    cooc = A.co_occurrence("HRV low", "sleep short")
    assert cooc == "HRV low while sleep short"
    assert "because" not in cooc and "matches" not in cooc


def test_engine_emits_only_sanctioned_causal_phrasings():
    stmts = _all_engine_statements()
    # every rule id is reachable and none is left unphrased
    assert set(stmts) == set(A.RULE_IDS)

    sanctioned = {
        "late_bedtimes": r"^matches late bedtimes, not load$",
        "load_driven": r"^matches load, not schedule$",
        "heat_ef": r"^EF suppressed by heat, not lost fitness$",
        "load_hrv_lag": r"tends to be followed by (lower|higher) HRV next day "
                        r"\(r=[-\d.]+, correlation not causation\)$",
        "debt_hrv_lag": r"tends to be followed by (lower|higher) HRV next day "
                        r"\(r=[-\d.]+, correlation not causation\)$",
    }
    for rule, statement in stmts.items():
        assert re.search(sanctioned[rule], statement), f"{rule}: unsanctioned phrasing {statement!r}"
        # the sanctioned vocabulary is "matches …/suppressed by …/tends to …",
        # never a free "because"
        assert "because" not in statement.lower(), f"{rule} leaks a bare 'because'"


def test_glossary_documents_exactly_the_engine_rules():
    entry = reference._ENTRIES["attribution_rules"]
    blob = " ".join(str(v) for v in vars(entry).values() if v)
    assert "closed" in blob.lower() and "because" in blob.lower()
    assert "five" in blob.lower(), "glossary must name the count of rules"
    assert len(A.RULE_IDS) == 5
    # the glossary interpolates the engine's own thresholds, so a constant
    # change shows up in both places at once — assert one is present verbatim
    assert f"{A.BEDTIME_VARIANCE_HIGH_H:g}" in blob
    assert f"{A.MIN_CORRELATION_DAYS}" in blob


@pytest.mark.parametrize("g", [g for g in GOLDENS if not g.name.startswith("reference")],
                         ids=lambda g: g.name)
def test_no_unsanctioned_because_reaches_a_render(g):
    """No shipped render states a cause with a bare 'because'. The glossary
    (excluded above) is the one place allowed to discuss the word itself."""
    assert not re.search(r"(?i)\bbecause\b", g.text), (
        f"{g.name} renders a bare 'because' — causal claims must go through "
        f"the attribution engine's closed phrasings (§8.7)"
    )

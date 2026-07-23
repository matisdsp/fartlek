"""Real-tokenizer budget gate (DESIGN §4.5) — the honest budget guarantee.

Phase-0 debt: the runtime estimator `renderer.estimate_tokens` is
`ceil(len/3.2)`, a single linear char-divisor. Measured against a real BPE
tokenizer (tiktoken cl100k_base) on the golden corpus, it is *not* an upper
bound — it undercounts dense numeric tables by up to ~32% (activity.full: est
292 vs real 432; week: est 386 vs real 475) and over-counts English prose by
~36% (reference.index: est 1171 vs real 863). No linear char model can be a
true upper bound on a BPE tokenizer (adversarial punctuation tokenises ~1
token/char), and forcing one would inflate every estimate enough to break the
near-limit catalog gate for no real safety.

So the gate asserts the guarantee that actually matters, not a formula that
cannot hold: **the real tokenizer count of every capped render fits that
tool's cap.** The cheap estimator remains what the renderer uses for
drop-ordering — fine, because every cap carries large real headroom (worst
observed utilisation is ~58%). A second, looser check keeps the estimator
honest as a heuristic (within a documented band of the real count) so a
renderer change that made it wildly misleading fails CI.

tiktoken is a dev-only dependency and needs its vocab (bundled offline in this
env; downloaded once in networked CI). If it cannot load, the gate skips with a
loud reason rather than passing vacuously.
"""
from __future__ import annotations

import pytest

from fartlek.render.renderer import estimate_tokens
from tests.golden_renders import GOLDENS

tiktoken = pytest.importorskip("tiktoken")


@pytest.fixture(scope="module")
def real_tokens():
    """cl100k_base token counter, or skip if the vocab is unreachable."""
    try:
        enc = tiktoken.get_encoding("cl100k_base")
        return lambda text: len(enc.encode(text))
    except Exception as exc:  # offline with a cold cache
        pytest.skip(f"tiktoken vocab unavailable ({exc}); gate needs network or a warm cache")


# The capped rendering tools the corpus must never silently stop covering.
EXPECTED_TOOLS = {
    "brief", "recovery", "load", "fitness", "week", "whats_changed",
    "reference", "activity",
}


def test_corpus_covers_every_capped_rendering_tool():
    """Guards against a vacuous pass if a render is dropped from the corpus."""
    covered = {g.name.split(".")[0] for g in GOLDENS}
    assert EXPECTED_TOOLS <= covered, f"corpus lost coverage of {EXPECTED_TOOLS - covered}"


@pytest.mark.parametrize("g", GOLDENS, ids=lambda g: g.name)
def test_real_render_fits_its_real_cap(g, real_tokens):
    """The budget contract: the real tokenizer count fits the tool's cap.

    This is what protects a client's context window — not the estimate, which
    can undercount. Enforced on the widest render each tool emits.
    """
    real = real_tokens(g.text)
    assert real <= g.cap, (
        f"{g.name}: real tokenizer count {real} exceeds cap {g.cap} "
        f"(estimator said {estimate_tokens(g.text)} — it undercounts dense tables)"
    )


# The estimator's observed behaviour on this corpus is est/real ∈ [0.68, 1.36].
# The band below is deliberately wider: it is a regression tripwire, not a
# contract — it fails only if a renderer change makes the cheap estimate a
# wildly misleading proxy (which would corrupt drop-ordering), while the hard
# guarantee above stays independent of it.
_EST_UNDERCOUNT_FLOOR = 0.60   # estimate never below 60% of real
_EST_OVERCOUNT_CEIL = 1.50     # estimate never above 150% of real


@pytest.mark.parametrize("g", GOLDENS, ids=lambda g: g.name)
def test_estimator_stays_a_sane_heuristic(g, real_tokens):
    real = real_tokens(g.text)
    ratio = estimate_tokens(g.text) / real
    assert _EST_UNDERCOUNT_FLOOR <= ratio <= _EST_OVERCOUNT_CEIL, (
        f"{g.name}: estimator/real ratio {ratio:.2f} left the sane band "
        f"[{_EST_UNDERCOUNT_FLOOR}, {_EST_OVERCOUNT_CEIL}] — drop-ordering would misbehave"
    )

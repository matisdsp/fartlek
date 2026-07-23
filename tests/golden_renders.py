"""Golden render corpus — deterministic worst-case renders per capped tool.

One place that produces a representative *widest* render for every tool that
emits a token-capped report, plus a banner variant for the tools that carry the
active-alert banner (the ⚠ glyph tokenises to 3 tokens for 1 char, so a banner
is a genuine stress on any char-based estimate).

These are generated from the existing per-tool test harnesses (same seeds, same
FakeContexts) rather than frozen to disk: the seeds are deterministic (fixed
dates, no clock/RNG), so the corpus is reproducible without a regen dance, and
it drifts automatically with the renderers it exercises.

Consumed by `test_budget_gate.py`, whose contract is the honest one: the real
tokenizer count of each render must fit the tool's real cap. The cheap runtime
estimator (`renderer.estimate_tokens`) is a heuristic for drop-ordering, not a
proven upper bound — see that test and DESIGN §4.5.

Coverage note (invariant §8.5 — no silent caps): this corpus covers the capped
*rendering* tools — brief, recovery, load, fitness, week, whats_changed,
reference, activity (all three detail tiers). The tiny write confirmations
(log/set_profile/sync, caps ≤200) and the raw JSON passthrough (its own
re-downsampling loop enforces its 5,000 cap at runtime) are out of scope.
"""
from __future__ import annotations

import asyncio
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

from fartlek.analytics import fusion
from fartlek.mcp_server.tools import (
    activity,
    brief,
    fitness,
    recovery,
    reference,
    week,
    whats_changed,
)
from fartlek.mcp_server.tools import (
    load as load_tool,
)
from fartlek.store import Store

# Per-tool test harnesses (seeds + FakeContexts). Importing sibling test
# modules is deliberate: the corpus stays representative of what the suite
# actually exercises, and a changed seed signature breaks here loudly.
from tests import test_tool_activity as AC
from tests import test_tool_brief as BR
from tests import test_tool_fitness as FT
from tests import test_tool_load as LD
from tests import test_tool_recovery as RC
from tests import test_tool_reference as RF
from tests import test_tool_week as WK
from tests import test_tool_whats_changed as WC

BANNER = "⚠ ACTIVE (since Thu 07-17): HRV below band + RHR elevated — see garmin_recovery()"


@dataclass(frozen=True)
class Golden:
    name: str      # stable label, e.g. "week.banner"
    cap: int       # the tool's real token cap this render must fit
    text: str      # the rendered markdown


def _store() -> Store:
    return Store(Path(tempfile.mkdtemp()) / "golden.db")


@contextmanager
def _fused(verdict: str = "GREEN"):
    """Drive brief through a full-marker readiness, the widest report it emits
    (mirrors the sanctioned test double in test_tool_brief.patch_fusion)."""
    readiness = {
        "verdict": verdict, "rationale": "fused",
        "markers_used": ["hrv", "rhr", "sleep", "form", "body_battery"],
        "provisional": False, "provisional_n": None, "gated_by": None,
        "modification": None,
    }
    with mock.patch.object(fusion, "marker_inputs", lambda _store, date: {"date": date}), \
         mock.patch.object(fusion, "compute_readiness", lambda _inputs: dict(readiness)), \
         mock.patch.object(fusion, "apply_gates", lambda r, _logs, _inputs: r):
        yield


def _build() -> list[Golden]:
    out: list[Golden] = []

    def add(name: str, cap: int, text: str) -> None:
        assert text and "VERDICT" in text, f"{name}: not a rendered report"
        out.append(Golden(name, cap, text))

    # brief — widest full-marker report + banner
    with _store() as s:
        BR.seed_full(s)
        BR.seed_yesterday_run(s)
        with _fused():
            add("brief", brief.CAP, asyncio.run(brief.run(BR.FakeContext(s))))
            add("brief.banner", brief.CAP,
                asyncio.run(brief.run(BR.FakeContext(s, banner=BANNER))))

    # recovery — 90-day window (widest) + banner
    with _store() as s:
        RC.seed(s, nights=90)
        RC.seed_timeline(s)
        add("recovery", recovery.CAP, asyncio.run(recovery.run(RC.FakeContext(s), days=90)))
        add("recovery.banner", recovery.CAP,
            asyncio.run(recovery.run(RC.FakeContext(s, banner=BANNER), days=90)))

    # load — 52-week window (widest) + banner
    with _store() as s:
        LD.seed_calm(s, days=400)
        add("load", load_tool.CAP, asyncio.run(load_tool.run(LD.FakeContext(s), weeks=52)))
        add("load.banner", load_tool.CAP,
            asyncio.run(load_tool.run(LD.FakeContext(s, banner=BANNER), weeks=52)))

    # fitness — 52-week window with a long run + many sessions + banner
    with _store() as s:
        FT.seed_band(s)
        FT.seed_long_run(s, stopped_s=600.0)
        for i in range(20):
            FT.seed_run(s, f"2026-0{4 + i % 3}-{1 + i % 28:02d}", 3000 + i, hr=150 - i % 10)
        add("fitness", fitness.CAP, asyncio.run(fitness.run(FT.FakeContext(s), weeks=52)))
        add("fitness.banner", fitness.CAP,
            asyncio.run(fitness.run(FT.FakeContext(s, banner=BANNER), weeks=52)))

    # week — a full mixed week (the densest numeric table) + banner
    with _store() as s:
        WK.seed_week(s)
        add("week", week.CAP,
            asyncio.run(week.run(WK.FakeContext(s), anchor_date="2026-07-13")))
        add("week.banner", week.CAP,
            asyncio.run(week.run(WK.FakeContext(s, banner=BANNER), anchor_date="2026-07-13")))

    # whats_changed — many significant deltas + banner
    with _store() as s:
        WC.seed_many_significant(s)
        add("whats_changed", whats_changed.CAP,
            asyncio.run(whats_changed.run(WC.FakeContext(s))))
        add("whats_changed.banner", whats_changed.CAP,
            asyncio.run(whats_changed.run(WC.FakeContext(s, banner=BANNER))))

    # reference — the metrics-glossary index (its only render; the per-metric
    # `_ENTRIES` are rows inside this one page, not separate topics)
    add("reference.index", reference.CAP, asyncio.run(reference.run(RF.FakeContext())))

    # activity — all three detail tiers (splits/full are dense lap tables)
    with _store() as s:
        AC.seed_default(s)
        raw = {AC.SPLITS_PATH: AC.TYPED_SPLITS, AC.DETAILS_PATH: AC.DETAILS}
        std_cap, splits_cap, full_cap = (activity.CAPS[k] for k in ("standard", "splits", "full"))
        add("activity.standard", std_cap,
            asyncio.run(activity.run(AC.FakeContext(s), activity_id=AC.A_ID)))
        add("activity.splits", splits_cap,
            asyncio.run(activity.run(AC.FakeContext(s, raw=raw),
                                     activity_id=AC.A_ID, detail="splits")))
        add("activity.full", full_cap,
            asyncio.run(activity.run(AC.FakeContext(s, raw=raw),
                                     activity_id=AC.A_ID, detail="full")))

    return out


GOLDENS: list[Golden] = _build()

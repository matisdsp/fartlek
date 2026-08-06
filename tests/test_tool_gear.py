"""garmin_gear tool tests — hermetic, FakeContext over a temp Store."""
from __future__ import annotations

from typing import Any

from fartlek.mcp_server.tools import gear

TODAY = "2026-08-06"  # a Thursday
TS = "2026-08-06T07:00:00"


class FakeContext:
    def __init__(self, store, today: str = TODAY, banner: str | None = None):
        self.store = store
        self._today = today
        self._banner = banner
        self.ready_calls = 0

    async def ensure_ready(self) -> None:
        self.ready_calls += 1

    async def ensure_fresh_today(self) -> None:
        raise AssertionError("gear must not force a today-refresh")

    def today(self) -> str:
        return self._today

    def data_as_of(self) -> str:
        return "07:41"

    def banner(self) -> str | None:
        return self._banner

    async def fetch_raw(self, path: str, **params: Any) -> Any:
        raise AssertionError("no network in tests")

    async def run_sync(self, backfill_days: int = 0) -> dict[str, Any]:
        raise AssertionError("no network in tests")


def add_gear(store, uuid, name, *, total=None, max_m=1_000_000.0, **kw):
    row = {"uuid": uuid, "name": name, "type": "shoes", "status": "active",
           "max_meters": max_m, "total_meters": total, "date_begin": "2026-01-01",
           "synced_at": TS}
    row.update(kw)
    store.upsert_gear(row)


def add_session(store, aid, date, uuid=None, *, distance=10_000.0, sport="running"):
    store.upsert_activity({"activity_id": aid, "date": date, "sport": sport,
                           "distance_m": distance, "synced_at": TS})
    if uuid is not None:
        store.replace_activity_gear(aid, [uuid])


# --- empty states ------------------------------------------------------------

async def test_never_synced_says_so(store):
    out = await gear.run(FakeContext(store))
    assert "has not synced yet" in out and "garmin_sync()" in out


async def test_no_gear_in_connect_points_at_where_to_add_it(store):
    store.set_capability("gear", False, "no gear on file")
    out = await gear.run(FakeContext(store))
    assert "No gear on file in Garmin Connect" in out
    assert "Gear → Add Gear" in out


async def test_all_retired_offers_the_flag(store):
    add_gear(store, "u1", "Old pair", total=1_100_000.0, status="retired")
    out = await gear.run(FakeContext(store))
    assert "Every item in the locker is retired" in out
    assert "garmin_gear(include_retired=True)" in out
    # and the flag actually shows them
    shown = await gear.run(FakeContext(store), include_retired=True)
    assert "Old pair" in shown


async def test_empty_states_keep_the_banner(store):
    ctx = FakeContext(store, banner="⚠ ACTIVE (since Mon 08-03): RHR up — see garmin_recovery()")
    out = await gear.run(ctx)
    assert out.startswith("⚠ ACTIVE")


# --- the locker table --------------------------------------------------------

async def test_render_shape(store):
    add_gear(store, "u1", "Bondi 9 I", total=918_000.0)
    add_session(store, 1, "2026-07-31", "u1", distance=21_000.0)
    out = await gear.run(FakeContext(store))

    assert "# Gear — Thu 2026-08-06 (data as of 07:41)" in out
    assert "| Gear | km | of limit | Last worn | 90d |" in out
    assert "| Bondi 9 I | 918 | 92% |" in out
    assert "Fri 07-31" in out                 # last worn, weekday form
    assert "1 shoes" in out                   # inventory in the verdict


async def test_verdict_names_the_pair_that_needs_a_decision(store):
    add_gear(store, "u1", "Bondi 9 I", total=1_012_000.0)
    add_gear(store, "u2", "Superblast 3", total=120_000.0)
    out = await gear.run(FakeContext(store))
    verdict = next(line for line in out.splitlines() if line.startswith("**VERDICT"))
    assert "Bondi 9 I" in verdict and "replace" in verdict
    assert "Superblast 3" not in verdict


async def test_worst_first_ordering_in_the_table(store):
    add_gear(store, "u1", "Fresh", total=50_000.0)
    add_gear(store, "u2", "Done", total=1_050_000.0)
    add_gear(store, "u3", "Ageing", total=790_000.0)
    out = await gear.run(FakeContext(store))
    body = out[out.index("| Gear |"):]
    assert body.index("Done") < body.index("Ageing") < body.index("Fresh")


async def test_gear_without_a_limit_renders_a_dash_not_a_default(store):
    add_gear(store, "u1", "No limit set", total=900_000.0, max_m=None)
    out = await gear.run(FakeContext(store))
    assert "| No limit set | 900 | — |" in out
    assert "no limit to judge it against" in out


async def test_unworn_gear_renders_dashes(store):
    add_gear(store, "u1", "Boxed", total=None)
    out = await gear.run(FakeContext(store))
    assert "| Boxed | — | — | — | — |" in out


async def test_rotation_share_is_of_the_same_gear_type(store):
    add_gear(store, "u1", "Daily", total=400_000.0)
    add_gear(store, "u2", "Racer", total=100_000.0)
    add_gear(store, "u3", "Commuter", total=2_000_000.0, type="bike", max_m=None)
    add_session(store, 1, "2026-08-01", "u1", distance=30_000.0)
    add_session(store, 2, "2026-08-02", "u2", distance=10_000.0)
    add_session(store, 3, "2026-08-03", "u3", distance=100_000.0, sport="cycling")
    out = await gear.run(FakeContext(store))
    assert "30 (75%)" in out      # 30 of the 40 shoe km
    assert "10 (25%)" in out
    assert "100 (100%)" in out    # the bike is alone in its type


async def test_the_90d_window_excludes_older_sessions(store):
    add_gear(store, "u1", "Pair", total=500_000.0)
    add_session(store, 1, "2026-08-01", "u1", distance=12_000.0)
    add_session(store, 2, "2026-01-01", "u1", distance=99_000.0)  # outside 90d
    out = await gear.run(FakeContext(store))
    assert "12 (100%)" in out
    assert "99" not in out


# --- honesty -----------------------------------------------------------------

async def test_method_note_attributes_the_limits_to_the_athlete(store):
    add_gear(store, "u1", "Pair", total=500_000.0)
    out = await gear.run(FakeContext(store))
    assert "set in Garmin Connect" in out
    assert "watch 75%, replace 100%" in out


async def test_partial_attribution_is_disclosed(store):
    """A 90d column resting on 1 of 3 sessions must not read as a total."""
    add_gear(store, "u1", "Pair", total=500_000.0)
    add_session(store, 1, "2026-08-01", "u1")
    add_session(store, 2, "2026-08-02")   # no gear link yet
    add_session(store, 3, "2026-08-03")
    out = await gear.run(FakeContext(store))
    assert "90d column covers 1 of 3 sessions" in out


async def test_full_attribution_says_nothing(store):
    add_gear(store, "u1", "Pair", total=500_000.0)
    add_session(store, 1, "2026-08-01", "u1")
    out = await gear.run(FakeContext(store))
    assert "90d column covers" not in out


async def test_a_long_locker_discloses_the_tail_it_does_not_print(store):
    """Bounded, never silently (§8.5) — and what falls off is always the gear
    furthest from a decision, because the table is sorted worst-first."""
    for i in range(11):
        add_gear(store, f"u{i}", f"Pair {i}", total=100_000.0 * (i + 1))
    out = await gear.run(FakeContext(store))

    assert out.count("| Pair ") == gear.MAX_ROWS
    assert "3 further items not shown" in out
    assert "Pair 10" in out and "Pair 0" not in out   # worst kept, freshest cut


async def test_a_single_extra_item_reads_in_the_singular(store):
    for i in range(gear.MAX_ROWS + 1):
        add_gear(store, f"u{i}", f"Pair {i}", total=100_000.0 * (i + 1))
    out = await gear.run(FakeContext(store))
    assert "1 further item not shown" in out


# --- plumbing ----------------------------------------------------------------

async def test_ensure_ready_is_called(store):
    ctx = FakeContext(store)
    await gear.run(ctx)
    assert ctx.ready_calls == 1


async def test_next_steps_drop_the_retired_hint_once_it_is_on(store):
    add_gear(store, "u1", "Pair", total=500_000.0)
    plain = await gear.run(FakeContext(store))
    assert "garmin_gear(include_retired=True)" in plain
    with_retired = await gear.run(FakeContext(store), include_retired=True)
    assert "garmin_gear(include_retired=True)" not in with_retired


async def test_render_fits_the_cap_with_a_crowded_locker(store):
    """Ten pairs, banner, full method note — the widest realistic render."""
    for i in range(10):
        add_gear(store, f"u{i}", f"Very Long Shoe Name {i}", total=100_000.0 * (i + 1))
        add_session(store, i + 1, "2026-08-01", f"u{i}", distance=15_000.0)
    ctx = FakeContext(store, banner="⚠ ACTIVE (since Mon 08-03): RHR up — see garmin_recovery()")
    out = await gear.run(ctx)
    from fartlek.render.renderer import estimate_tokens
    assert estimate_tokens(out) <= gear.CAP_TOKENS

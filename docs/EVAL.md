# Evaluation harness — v0.2 (reduced)

*Scope decision 2026-07-23: v0.2 ships with a **reduced** eval harness — ~10 multi-tool coaching tasks played locally on Claude Code, at least one in French, verifying number preservation and no fabrication. The **full** programme (30 tasks × 3 clients — Claude Code / Desktop / Cursor — token & calls-per-task regression gates, and formal transcript audits where every LLM-re-derived number = a missing pre-computation) is **v0.2.1**. See `PHASE2.md` §4 and `DESIGN.md` §4.5.*

This file is the durable harness: the task set, the acceptance criteria, and the record of the v0.2 live run against the maintainer's real account. v0.2.1 automates and expands it.

---

## Acceptance criteria (per task)

A task passes when the assistant's answer:

1. **Re-derives no number.** Every figure stated comes verbatim from a tool render — the model never recomputes a statistic or aggregate (the central bet, `HANDOFF` §1).
2. **Fabricates nothing.** An absent metric is stated absent, never defaulted or invented (invariant §8.5). Provisional/low-confidence renders are relayed *as* provisional.
3. **Routes coherently.** The tools invoked match the intent (routing table in the server instructions); the entry point for "today" is `garmin_brief`, browsing is `garmin_activities`, etc.
4. **Honours athlete primacy.** A reported illness/injury/exhaustion caps the advice regardless of a GREEN sensor verdict, and prompts `garmin_log` (invariant §8.4).
5. **Preserves numbers across language.** The server renders English; when the client answers in another language, every number and unit survives the translation.
6. **Stays numerically coherent across tools** — or, where two tools legitimately differ (different window/anchor/basis), the difference is disclosed, not silently contradictory. *(This is the criterion the v0.2 run stresses hardest — see Findings.)*

---

## Task set

| # | Lang | Prompt (paraphrased) | Expected routing | Primary check |
|---|---|---|---|---|
| A | EN | "Should I train today?" | `garmin_brief` | 1, 3 — readiness verdict, markers named |
| B | EN | "Am I overtraining? Catch me up on anything I should know." | `garmin_recovery` + `garmin_whats_changed` | 1, 2, 3 — ≥2-of-3 group rule, only significant changes |
| C | EN | "Am I on track for my 24h race?" | `garmin_fitness` (+ `garmin_athlete` goal) | 1, 2 — range not point, assumptions disclosed |
| D | **FR** | "Est-ce que je m'entraîne trop en ce moment ? Donne-moi les chiffres." | `garmin_load` | **5** — numbers preserved in a French answer |
| E | EN | "How's my recovery, and how is ACWR computed?" | `garmin_recovery` + `garmin_reference(metric="acwr")` | 1, 3 — explainability, provenance flags |
| F | EN | "How was last week? Break down my hardest session." | `garmin_week` + `garmin_activity` | 1, 3 — per-day table → session depth |
| G | EN/FR | "I feel feverish and wiped out — should I do my workout?" | advise caution + offer `garmin_log`; sensors do **not** override | 4 — athlete primacy |
| H | EN | "Compare this week's intensity distribution to my norm." | `garmin_load` / `garmin_week` TID | 1, 6 — TID own-norm, drift disclosed |
| I | EN | "What changed in the last month?" | `garmin_whats_changed(since_days=30)` | 2 — significance gate, "nothing notable" path |
| J | FR | "Fais-moi le point du matin." | `garmin_brief` | 3, 5 — French morning brief, numbers intact |

Tasks A–F were executed live in the v0.2 run below (covering routing, explainability, session depth, French, and the multi-tool consistency surface). G is specified but **not** run live: it would write a (false) illness log to the real account — its correct behaviour (advise caution, offer to log, never let a GREEN sensor override) is asserted by `tests/test_tool_recovery.py::test_logged_illness_today_caps_the_verdict_however_calm_the_sensors` and the brief illness-gate tests. H–J are queued for the v0.2.1 expansion.

---

## v0.2 live run — 2026-07-23, real account (`b2db9a6f…`, 207 days synced)

Read-only; no writes to the account. Tools returned pre-computed renders; the assistant relayed them without recomputation.

| # | Tools invoked | Result |
|---|---|---|
| A | `garmin_brief` | ✅ AMBER — "replace quality with 40 min easy" on a 4h02 / deep-0 / score-41 night, form +8%. Markers named. No fabrication (Body Battery n=1 disclosed). |
| B | `garmin_recovery(28)` + `garmin_whats_changed(7)` | ✅ "1 of 3 marker groups deviant — single group, not an alarm" (the ≥2-of-3 rule held); changes limited to the 2 significant ones (RHR ↓3.73 p=0.019, HRV ↑ p=0.020), EF/Body-Battery correctly withheld as "<21 points". |
| C | `garmin_fitness(12)` | ✅ 24h projects **187–204 km**, target 200 inside range; range-not-point, exponent band flagged population-default, 3.8% stoppage stated, reference effort labelled sub-maximal. |
| D | `garmin_load(8)` | ✅ see French answer below — every number preserved. |
| E | `garmin_recovery` + `garmin_reference(acwr)` | ✅ ACWR explained: EWMA(7):EWMA(28), guard thresholds flagged population-default vs personally-derived EWMA, "contested … spike detector, never a standalone verdict." |
| F | `garmin_week` + `garmin_activity(latest run)` | ✅ per-day table with activity_ids → session depth ("Puteaux Running", 6.9 km, comparable-effort verdict vs closest past run). Compliance correctly absent (no plan matched). |

**Criteria 1–4 pass across A–F.** Criterion 5 passes (task D). Criterion 6 is where the run earns its keep — see below.

### Task D — French answer (evidence for criterion 5)

Source render (`garmin_load`, English): CTL 84→102 (+21% / +2.7%/wk), ATL 84, TSB +8%, ramp −3.6%/wk, ACWR 0.73, monotony 0.9–1.6, TID own-norm 93/5/3 vs recent 100/0/0, precedent monotony 1.2 vs 1.87.

> **Charge sur 8 semaines.** Ta forme de fond (CTL) est passée de 84 à 102 (+21 % sur 8 semaines, soit +2,7 %/sem) — tu construis bien. Fatigue (ATL) 84, forme (TSB) +8 % du CTL : frais, presque en jour de course. Le ramp est à −3,6 %/sem, **sous** ta zone de build habituelle de 4–8 %/sem : tu ne surcharges pas, tu es plutôt en train de relâcher. ACWR 0,73 (sous la bande 0,8–1,3, un détecteur de pic contesté, pas un verdict). Monotonie 0,9–1,6 : aucun pic. Seul point à gérer : ta répartition d'intensité a dérivé de ta norme (93/5/3 easy/modéré/dur) vers 100/0/0 sur les 2 dernières semaines — 100 % facile. **Réponse : non, tu ne t'entraînes pas trop en ce moment.** Précédent perso : monotonie 1,2, loin de ton niveau pré-incident de 1,87.

Every figure (84, 102, +21 %, +2,7 %, 84, +8 %, −3,6 %, 4–8 %, 0,73, 0,8–1,3, 0,9–1,6, 93/5/3, 100/0/0, 1,2, 1,87) is carried through; only the decimal separator is localised. ✅

---

## Findings — cross-tool numeric coherence (criterion 6)

The numbers *within* each tool are internally consistent, and CTL (102), TSB (+8 %), RHR (44 median / 40 today), weekly load (584), and monotony (1.2) agree across every tool that reports them. The concern is the metrics where two tools show **different values or framings for what a user reads as "the same" number**, because the product thesis is that the server, not the model, owns the numbers:

| # | Metric | Divergence | Likely cause | Severity |
|---|---|---|---|---|
| E1 | HRV vs band | `brief` "above band" · `recovery` "in band" (101 ms 7d roll) · `week` "HRV left band this week / in band 1/2" | different band **windows** (brief: 60d band 84–95; recovery/week: shorter rolling band that has risen with the recent HRV uptrend). `recovery` does **not print its band bounds**, so the disagreement is invisible to the reader. | **medium** — three framings of one metric; the transparency gap (unprinted recovery band) is the fixable part |
| E2 | Sleep need | `brief`/`athlete` 8h00 · `week` 8.8h | baseline need (60d) vs current dynamic Garmin `sleepNeed` (this week's avg). Both defensible; neither labels which basis it is. | **medium** — a 48-min gap on the denominator of sleep debt, unlabelled |
| E3 | 14-day sleep debt | `recovery` 32.8h (12 nights) · `week` 20.9h | different night counts **and** the E2 need difference compound | low–medium — follows from E2 + window |
| E4 | ACWR | `load` 0.73 (EWMA 7:28, anchored today) · `week` 0.38 (in-progress week) | correct-by-design different anchors; both label EWMA but not the anchor | low — each is right for its window |

**Recommendation.** None is a number-loss or fabrication (criteria 1–2 hold), so none blocks the v0.2 *gate* work. But E1 and E2 are the kind of "subtly confusing across a session" issue this project treats seriously (`HANDOFF` §7: a wrong-feeling number erodes trust). Suggested for a focused pass — **v0.2 if the maintainer wants coherence tight before the flagship, else v0.2.1**:
- E1: have `garmin_recovery` (and `garmin_week`) **print the band bounds** they compare against, and reconcile whether the HRV-vs-band window should be shared across tools (candidate for a shared resolver like `_zones.resolve()`).
- E2: label sleep need as *baseline* vs *current* wherever it drives a debt figure, or standardise on one basis.

---

## Deferred to v0.2.1

- Tasks H–J executed live; the full 30-task set.
- Three clients (Claude Code **+ Desktop + Cursor**) — this run is Claude Code only.
- Automated token- and calls-per-task **regression gates** (this run is a manual read).
- Formal **transcript audits**: every model-stated number traced to the render that produced it (every re-derived number = a missing pre-computation).
- Coherence fixes E1–E4 if not taken in v0.2.

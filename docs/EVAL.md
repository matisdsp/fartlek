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

Each was traced to root cause by a dedicated investigation (2026-07-23); the table records the **corrected** cause, not the first-pass guess.

| # | Metric | Divergence | Root cause (investigated) | Disposition |
|---|---|---|---|---|
| E1 | HRV vs band | `brief` "⚠ above band" · `recovery` "in band" · `week` "in band 1/2" | *Not* different windows — **both use a 60d band.** `brief` is two-sided, `recovery` is floor-only. The real defect: brief's ⚠ on high HRV is a favorable-direction false positive — fusion treats high HRV as neutral ("above is never credited", §3.2 #8) and the alert scanner already tuned this exact case out. Display-only (never touched the verdict). | ✅ **fixed in v0.2** — brief renders above-band as ✓ (label kept). Fuller harmonisation (print recovery's band bounds, shared resolver) → v0.2.1 |
| E2-B | 14-day sleep debt | `recovery` 32.8h · `week` 20.9h | Same `sleep_debt(window=14)`; `week._recovery` anchored the trailing window at the future ISO-week Sunday, so it counted a different 14 nights than recovery run the same day. | ✅ **fixed in v0.2** — trailing windows clamped to `min(end, today)` |
| E2-A | Sleep need | `brief`/`athlete` 8h00 · `week` 8.8h | One `sleep_need_h` column; the values are a point value (today/latest) vs a 14-night mean, both valid. Concrete defect: `athlete` showed the *latest single night* under a "Baselines (60d)" header. | ✅ **fixed in v0.2** — athlete uses the 60d median; brief's per-night "today's need" is correct as-is |
| E4 | ACWR | `load` 0.73 (anchored today) · `week` 0.38 (in-progress week) | Correct-by-design different anchors; both label EWMA. | ⬜ by-design; optional: label the anchor. v0.2.1 |

**Outcome.** None was a number-loss or fabrication (criteria 1–2 always held). The two real bugs (E1 false-⚠, E2-B debt anchor) and the one mislabel (E2-A) were fixed in v0.2 with regression tests; E4 is by-design and E1's fuller band-transparency harmonisation is deferred to v0.2.1. This is the eval harness doing its job: run on real data, it caught three flagship-surface coherence defects that all unit tests had passed.

---

## Deferred to v0.2.1

- Tasks H–J executed live; the full 30-task set.
- Three clients (Claude Code **+ Desktop + Cursor**) — this run is Claude Code only.
- Automated token- and calls-per-task **regression gates** (this run is a manual read).
- Formal **transcript audits**: every model-stated number traced to the render that produced it (every re-derived number = a missing pre-computation).
- Remaining coherence work: only E4's anchor labelling (minor, by-design). **E1 is fully closed** — the shared HRV-band resolver (`baselines.hrv_band`, canonical 60d lnRMSSD mean ± 0.5·MAD-SD, §3.2 #8) now backs brief/recovery/week/fusion, so they can no longer diverge; the false-⚠, band-transparency, E2-A mislabel, and E2-B anchor bug were all fixed post-v0.2.

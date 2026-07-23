# Handoff — état du projet Fartlek

*Dernière mise à jour : 2026-07-23. Le moteur Phase 2 et les 6 outils sont terminés ; reste le durcissement qualité (gates CI) et la release v0.2.*

Ce document est le **point d'entrée** pour un agent (ou un humain) qui prend le relai. Il dit **où en est le projet**, **ce qui est vérifié**, **ce qui reste**, et **les pièges qui coûtent du temps**. Il ne duplique pas la spec : l'autorité reste `docs/DESIGN.md` (le quoi/pourquoi), `ROADMAP.md` (le plan par phase) et `docs/PHASE2.md` (la checklist item-par-item de la Phase 2, à jour dans le même commit que le travail).

---

## 0. TL;DR — commence ici

1. **Lis, dans l'ordre :** ce fichier → `docs/PHASE2.md` (checklist exacte du restant) → `docs/DESIGN.md` §3.2 (le catalogue de métriques, contrat) → `CLAUDE.md` (discipline projet).
2. **Fais tourner** `uv run pytest -q` (attendu : **915 passent**) et `uv run ruff check fartlek/ tests/` (clean).
3. **Le moteur et les 14 outils sont finis et vérifiés sur un compte Garmin réel.** Ce qui reste pour tagger la v0.2 : quelques **gates CI** (§6 ci-dessous) puis la release. Le gros programme d'éval part en **v0.2.1** (décision 2026-07-23).
4. **Un compte de test réel est installé** dans `~/.fartlek/` (voir §4). Ne le casse pas ; ne commite jamais `~/.fartlek/` ni `.env`.

---

## 1. Le projet en trois phrases

Fartlek est un serveur MCP qui transforme les données Garmin en un rapport de coach compact. Le pari central : **la synthèse se fait côté serveur, en Python déterministe** — pas de LLM côté serveur, pas de passthrough JSON brut. Le serveur livre des verdicts pré-calculés contre les baselines personnelles de l'athlète.

Corollaire structurant : **le LLM ne doit jamais avoir à re-dériver une statistique**. Si un nombre est recalculé côté modèle, c'est un bug de conception. Et son pendant : **ne rien fabriquer** — une métrique absente est annoncée absente, jamais inventée, jamais remplie d'un défaut déguisé en mesure.

---

## 2. État actuel — vérifié le 2026-07-23

| Élément | État |
|---|---|
| Phase 0 (foundation) | ✅ terminée |
| Phase 1 (core read surface, v0.1) | ✅ terminée, **0.1.1 sur PyPI** |
| **Phase 2 — moteur d'analyse** | ✅ **complet** (10 modules ; 2 items mineurs non bloquants — voir §5) |
| **Phase 2 — les 6 outils** | ✅ **livrés, câblés, vérifiés en MCP réel** |
| Détecteur d'alertes | ✅ calé sur 6 mois de données réelles (75 → 27 alertes) |
| Validation externe (intervals.icu) | ✅ decoupling croisé, écart médian 1 pt |
| Tests | ✅ **915 passent** (`uv run pytest -q`, ~4 s) |
| Lint | ✅ `uv run ruff check fartlek/ tests/` |
| Version PyPI en ligne | **0.1.1** (le bump 0.2.0 reste à faire) |
| Programme qualité / gates CI | ⬜ partiel — voir §6 |
| Prompts & ressources MCP | ⬜ non commencés (progressive enhancement, candidat v0.2.1) |

**14 outils exposés** en tout : 8 de Phase 1 (`garmin_brief`, `garmin_activities`, `garmin_activity`, `garmin_athlete`, `garmin_set_profile`, `garmin_log`, `garmin_sync`, `garmin_raw`) + 6 de Phase 2 (`garmin_recovery`, `garmin_load`, `garmin_fitness`, `garmin_week`, `garmin_whats_changed`, `garmin_reference`).

Vérifications réellement effectuées (pas supposées) :
- Les 14 outils apparaissent dans `tools/list` via le protocole MCP réel (smoke check §4).
- `garmin_recovery`, `garmin_fitness`, `garmin_load`, `garmin_week` rendus sur le compte réel et relus à la main.
- Le catalogue d'outils tient sous le plafond de 3 500 tokens (gate `test_catalog_under_budget`).

---

## 3. Architecture — le chemin des données

```
Garmin Connect API
      ↓  adapters/garmin_connect.py   (lib garminconnect ; sync dans asyncio.to_thread + lock fcntl)
      ↓  health/service.py            (filtrage de champs ; les consommateurs passent TOUJOURS par ici)
      ↓  sync/engine.py               (staleness, backoff 429, curseur resumable, capability probes,
      ↓                                tier0/1/2, backfill_splits, userstats range, zones+poids)
      ↓  store/store.py               (SQLite par compte, WAL)
      ↓  analytics/*.py               (le moteur déterministe — voir la table ci-dessous)
      ↓  mcp_server/tools/_zones.py   (résolution partagée des zones HR pour les 3 outils TID)
      ↓  render/renderer.py           (verdict grammar, budgets tokens, drop order, safety banner)
      ↓  mcp_server/tools/*.py        (14 outils)
      ↓  mcp_server/server.py         (FastMCP stdio)
```

### Le moteur `analytics/` — qui calcule quoi

| Module | Rôle | Spec |
|---|---|---|
| `pmc.py` | CTL/ATL/TSB, form bands, ACWR EWMA, monotony/strain ; `advance()` partagé avec la projection | §3.2 #1-4 |
| `baselines.py` | rolling mean/median/MAD-SD, z, band position, streak ; **RHR deviation two-sided** | §3.2 #6, #9 |
| `trends.py` | **significativité** : Hamed-Rao MK + Sen + SWC par métrique. Croisé vs `pymannkendall` | §3.2 #7 |
| `efficiency.py` | EF/decoupling/durability **par tour** ; **HR-at-pace par bande = mesure primaire** (amendement) | §3.2 #12, #13 |
| `sleep.py` | dette de sommeil, SRI (Phillips), décalage horaire social | §3.2 #10 |
| `tid.py` | répartition d'intensité 3-zones **pro-ratée** ; classify (incl. `base`) ; grey-zone creep ; `zone_mapping_kwargs` | §3.2 #11 |
| `convergence.py` | audit surentraînement : ≥2 groupes sur 3 pour alarmer ; hr_response corroborant | §3.2 #20 |
| `projection.py` | PMC forward (motif par jour de semaine) + fenêtre d'affûtage | §3.2 #17 |
| `race.py` | Riegel + fit d'exposant + **modèle temps-fixe** (fourchette, arrêts, `compare_to_field`) | §3.2 #16 + amendement |
| `attribution.py` | les **5 seuls** « parce que » autorisés ; silencieux si l'évidence ne tranche pas | §3.2 #22 |
| `precedent.py` | précédents personnels ; fusion multi-sources ; exclusion des épisodes externes | §3.2 #5 |
| `matcher.py` | planifié-vs-exécuté (Phase 1) | §3.2 #15 |
| `alerts.py` | scan d'anomalies → table `alerts` ; **calé sur données réelles** (voir §7) | §3.2 #21 |
| `fusion.py` | fusion readiness (cœur de `garmin_brief`) | §3.2 #18 |
| `load.py` | courbe de charge quotidienne + calibration + fallback | §3.1 |

Points d'entrée utiles : `mcp_server/context.py` (`ToolContext`, `ensure_ready()` cold start) · `render/renderer.py` (tout le formatage + budgets) · `mcp_server/tools/_zones.py` (le seul endroit qui résout les zones HR persistées → arguments TID).

---

## 4. Commandes & données de test

```bash
uv sync                          # install (dev inclus)
uv run pytest -q                 # 915 tests, ~4 s
uv run ruff check fartlek/ tests/
uv run fartlek auth --replace    # login Garmin (email/password + MFA) — NÉCESSITE UN VRAI TERMINAL
uv run fartlek doctor            # health check
uv run fartlek sync              # tier0+tier1 (ajouter --nights N pour le backfill sommeil/HRV)
uv run fartlek-mcp               # serveur MCP en stdio
```

**Compte de test réel installé.** `~/.fartlek/` contient les tokens Garmin et le store du compte `b2db9a6f-...` : **~205 jours (déc. 2025 → juil. 2026), ~295 activités, 2 188 tours de piste, 142 nuits de timeline sommeil**, zones HR et poids persistés. C'est la matière première de toutes les vérifications live.
- **Ne commite JAMAIS** `~/.fartlek/` ni le fichier `.env` à la racine (déjà git-ignorés).
- Les tokens ont un `di_token` de ~28 h et un `di_refresh_token` (voir piège D3). Si `fartlek doctor` dit « session expired », relance `uv run fartlek auth --replace` dans un vrai terminal.

**Clé intervals.icu** (validation externe) : dans `.env` sous `INTERVALS_ICU_API_KEY`, compte athlète `i649595`. ⚠️ **L'API renvoie 403 avec le User-Agent par défaut d'urllib** — il faut un UA de navigateur (le script `scratchpad/icu.py` d'une session précédente le montre). Leur CTL est en échelle TSS (≠ notre échelle Garmin-load) : **seuls les ratios et le decoupling/EF par séance sont comparables.**

Smoke check MCP autonome (appeler un outil) :

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"garmin_recovery","arguments":{"days":28}}}' \
  | (cat; sleep 25) | uv run fartlek-mcp 2>/dev/null
```

**Test live** : appels Garmin séquentiels, polis (≤1 req/2s en backfill, backoff sur 429). Garmin est joint **uniquement par le process de sync**.

---

## 5. Ce qui reste — moteur

Deux items, tous deux **mineurs et non bloquants** pour la v0.2 :

- **Tanda + triangulation à 3 modèles** (§3.2 #16) : `race.py` a Riegel et le fit d'exposant, mais Tanda (`Pm = 17.1 + 140·e^(−0.0053K) + 0.55P`) et la triangulation Garmin/Tanda/Riegel restent à écrire. **Impact limité** : la course cible du mainteneur est à temps fixe (le modèle temps-fixe, lui, est fait), et les PRs ne sont de toute façon pas persistés (voir ci-dessous). À faire quand un athlète à objectif *distance* sera testé.
- **Tendances capability-gated** (running tolerance / endurance score, §3.2 #23) : aucun capability probe n'existe encore pour ces champs ; omis proprement jusqu'à ce qu'un probe soit ajouté.

**Dépendance cachée à connaître :** les **PRs ne sont jamais persistés** (`sync/engine.py` ne fait que *probe* `personal_records`). Donc la branche distance de `garmin_fitness` lit les PRs depuis `athlete_profile` (clés `pr_5k`/`pr_10k`/`pr_half`/`pr_marathon` en `H:MM:SS`) — que rien n'écrit aujourd'hui. Riegel est donc dormant faute d'entrée. Persister les PRs au sync est le préalable à toute prédiction de course sur distance.

---

## 6. Ce qui reste — qualité & release (le chemin vers la v0.2)

**Décision de scope (2026-07-23) :** la v0.2 sort avec les **gates CI automatisés** + un **harnais d'éval réduit** ; le programme lourd (30 tâches × 3 clients, audits de transcript, tâches FR) part en **v0.2.1**. Détail dans `docs/PHASE2.md` §4.

**Gates CI à écrire pour la v0.2** (tous dans `tests/`, s'inspirer de `test_guardrails.py`) :
1. **Gate tokenizer réel (tiktoken)** — dette de Phase 0. Compter chaque golden render avec un vrai tokenizer ; asserter que l'estimateur runtime `ceil(chars/3.2)` ne sous-compte jamais le tokenizer sur le set golden. C'est le plus gros morceau : il faut d'abord produire des **golden renders** (sorties figées des 14 outils sur des fixtures).
2. **Test de langage d'attribution** — tout « because »/« matches » rendu doit correspondre à une règle de `attribution.RULE_IDS` ; sinon le build casse. `attribution.py` expose déjà `RULE_IDS` et `CO_OCCURRENCE_TEMPLATE` exprès.
3. **Cohérence description/signature** — chaque format/param nommé dans une description d'outil doit exister dans son schéma.
4. **Gate coût de session ≤17K** — somme des caps durs, un appel par outil aux arguments par défaut.

**Harnais d'éval réduit (v0.2)** : ~10 tâches multi-outils jouées **localement sur Claude Code seulement**, incluant au moins une tâche en français (le serveur rend l'anglais, le client traduit — vérifier qu'aucun nombre ne se perd). Les 30 tâches × 3 clients et les audits de transcript sont **v0.2.1**.

**Release v0.2** (procédure vérifiée en Phase 1, cf. §8) :
1. bump `version` dans `pyproject.toml` **et** les deux champs de `server.json`.
2. `uv sync` (répercute dans `uv.lock` — le CI utilise `--frozen`, ne pas l'oublier).
3. commit, `git tag v0.2.0 && git push origin main v0.2.0` → le workflow OIDC publie sur PyPI.
4. `mcp-publisher login github` (**device-code OAuth — nécessite un humain**) puis `mcp-publisher publish` pour le registre MCP.
5. Annuaires tiers (Glama, mcp.so, PulseMCP) — le mainteneur soumet.

---

## 7. Défauts & dettes — tracés dans PHASE2.md §6

**Corrigés en route** (chacun aurait pu produire un conseil faux sans lever d'erreur) :
- **D1/D6** : 7 scalaires quotidiens n'avaient qu'1 jour d'historique (le daily summary n'est fetché que pour aujourd'hui) → backfill via `userstats-service` (1 appel de plage par métrique, voir `USERSTATS_DAILY_METRICS`). Corrige aussi les lignes figées par un sync de milieu de journée.
- **D8** : zones HR et poids fetchés par tier0 mais jamais persistés → maintenant stockés ; les 3 outils TID pro-ratent via `_zones.resolve()`.
- **D4** : le qualifier « séance régulière » de la spec ne retenait que 21 séances / 201 → **amendement §3.2 #12** : les bandes d'allure deviennent la mesure primaire.
- **D5** : `digest_laps` traitait l'index de tour 0 comme absent (`or` sur un entier falsy).
- Deux bugs de `race.py` trouvés en construisant `garmin_fitness` : `fit_riegel_exponent` sans `raw_b` sur les retours dégénérés ; `fixed_time_projection` traitant `stoppage=None` comme 0 % **et l'annonçant comme mesuré**.

**Ouverts** (non bloquants) :
- **D7** : `body_battery_wake` n'a qu'1 jour d'historique (absent de userstats, l'endpoint dédié ne rend que high/low). Pèse 0.10 dans la fusion readiness.
- **D2** : `ACTIVITY_HISTORY_DAYS = 180` non paramétrable — un athlète sur cycle long ne voit pas sa saison entière.
- **D3** (à surveiller) : le premier `fartlek auth` avait persisté `di_refresh_token: null` → session morte à ~20 h. Le re-login en a stocké un correct ; vérifier que le refresh réécrit bien le fichier au fil du temps.

**Le calage du scanner d'alertes (§7.4) est fait et vaut d'être compris** : rejeu sur 116 jours réels → 75 alertes (une tous les 1,5 j, injouable). Trois règles décidées avec l'athlète : (a) seule la direction *défavorable* alerte (31 % des alertes signalaient une *amélioration*) ; (b) la baseline de charge n'utilise que les jours d'entraînement ; (c) le sommeil exige 2 nuits courtes consécutives. Résultat : 75 → 27, AMBER 27 → 4. **Ancré par un positif certifié** : l'athlète a eu une salmonellose le 2026-04-19..22 (5 marqueurs déviants) — `test_salmonella_episode_is_still_detected` interdit tout durcissement futur qui masquerait ce jour.

---

## 8. Contrats à ne pas casser (invariants de conception)

1. **Les formules sont des contrats.** Constantes PMC, EWMA ACWR, MAD `1.4826`, Foster, Hamed-Rao, Phillips SRI : implémentées comme spécifié, testées contre des valeurs connues (`trends` est même croisé vs `pymannkendall`). Ne pas « améliorer » sans mettre à jour la spec.
2. **stdout réservé au JSON-RPC.** Tout log sur stderr. Un `print()` égaré casse le protocole.
3. **Budgets tokens durs**, appliqués par le renderer avec troncature *annoncée*. Le **catalogue** des 14 outils tient sous 3 500 tokens (payé par chaque conversation de chaque client) — ne pas relever le plafond, resserrer les descriptions.
4. **L'athlète prime sur les capteurs.** Une maladie/blessure via `garmin_log` plafonne le verdict, jamais l'inverse. Vaut aussi pour l'historique (precedent mining).
5. **Ne rien fabriquer.** Métrique absente = annoncée absente (jamais de ligne « null », jamais de valeur par défaut déguisée en mesure — cf. le bug `stoppage`). Approximation autorisée **si déclarée** (cf. la note bucket-vs-pro-raté de TID).
6. **Un seul marqueur n'alarme jamais.** L'audit de surentraînement exige ≥2 groupes sur 3. Sur-alerter détruit la confiance autant que sous-alerter.
7. **Causalité fermée.** Les seuls « parce que » autorisés sont les 5 règles de `attribution.py` ; tout le reste est de la co-occurrence (« X while Y »).
8. **Jamais de secrets commités** (`~/.fartlek/`, `.env`, `garmin_tokens.json`).
9. **Discipline de commit** : un changement cohérent = un commit, message impératif scopé, tests verts avant. Mettre à jour `docs/PHASE2.md` dans le même commit que le travail.

---

## 9. Contexte athlète (le compte de test réel)

L'athlète (le mainteneur) prépare **les 24 heures de Villenave d'Ornon, 2026-08-29, objectif ferme 200 km** — une épreuve à **temps fixe** (d'où le modèle dédié : Riegel/Tanda y sont inapplicables). Faits vérifiés en mémoire projet (`fartlek-goal-race-24h-villenave`, `matis-personal-load-thresholds`) :
- **Seuils de surcharge personnels**, dérivés de 3 épisodes réels qu'il a catégorisés : charge hebdo **974**, strain 1817, monotonie 1.87. **Son prédicteur est le volume hebdomadaire, pas la monotonie** (son pire épisode avait la monotonie la plus basse et la charge la plus haute).
- 4 épisodes enregistrés dans son `wellness_log` ; la salmonellose y est marquée `EXTERNAL` pour être exclue des niveaux de charge.
- Projection actuelle de `garmin_fitness` : **187–204 km** (confiance basse, déclarée), 200 km dans la fourchette. Le budget d'arrêts est le levier le plus sensible.

Ces faits sont du contexte de test, pas des invariants de code — mais ils expliquent beaucoup de choix (le modèle temps-fixe, la comparaison precedent sur la charge hebdo, l'exclusion des épisodes externes).

---

## 10. Références

- `docs/DESIGN.md` — spec faisant autorité (§2 surface d'outils, §3 moteur, §3.2 catalogue + **amendement bandes d'allure**, §4 guidance, §5 format, §6 roadmap, §7 questions ouvertes).
- `docs/PHASE2.md` — **la checklist item-par-item du restant** (à lire juste après ce fichier).
- `ROADMAP.md` — plan par phase.
- `CLAUDE.md` — discipline projet (architecture, commandes, commits).
- Mémoire projet (recall automatique) : `garmin-coach-open-source-direction`, `fartlek-goal-race-24h-villenave`, `matis-personal-load-thresholds`.
- Repo : https://github.com/matisdsp/fartlek · PyPI : https://pypi.org/project/fartlek-mcp/
- `garth` est **déprécié** (Garmin a cassé son login en 2026) — tout passe par `garminconnect`, dont le source dans `.venv/.../garminconnect/` fait foi pour les endpoints.

# Handoff — état du projet Fartlek

*Dernière mise à jour : 2026-07-22. Reprend le projet là où la Phase 1 s'arrête.*

Ce document est le point d'entrée pour un agent (ou un humain) qui prend le relai. Il dit **où en est le projet**, **ce qui est vérifié**, **ce qui reste**, et **les pièges qui coûtent du temps**. Il ne duplique pas la spec : l'autorité reste `docs/DESIGN.md` (le quoi/pourquoi) et `ROADMAP.md` (le plan par phase).

---

## 1. Le projet en trois phrases

Fartlek est un serveur MCP qui transforme les données Garmin en un rapport de coach compact. Le pari central : **la synthèse se fait côté serveur, en Python déterministe** — pas de LLM côté serveur, pas de passthrough JSON brut (une nuit de sommeil brute = ~52K tokens, illisible pour un modèle). Le serveur livre des verdicts pré-calculés contre les baselines personnelles de l'athlète.

Corollaire structurant : **le LLM ne doit jamais avoir à re-dériver une statistique**. Si un nombre est recalculé côté modèle, c'est un bug de conception, pas une optimisation manquante.

---

## 2. État actuel — vérifié le 2026-07-22

| Élément | État |
|---|---|
| Phase 0 (foundation) | ✅ terminée |
| Phase 1 (core read surface) | ✅ terminée |
| Tests | ✅ 430 passent (`uv run pytest -q`) |
| Lint | ✅ `uv run ruff check fartlek/ tests/` |
| PyPI | ✅ `fartlek-mcp` **0.1.1** en ligne |
| GitHub | `matisdsp/fartlek`, public, CI verte |
| Registre MCP officiel | ⏳ **en cours** — voir §5, une étape manuelle reste |
| Phase 2 | ⬜ pas commencée |

Vérifications réellement effectuées (pas supposées) :
- `uvx --refresh --from fartlek-mcp fartlek-mcp` depuis un dossier neutre → le serveur démarre et sert les **8 outils**. La commande du README fonctionne pour un utilisateur qui part de zéro.
- Le token d'ownership `mcp-name:` est **lisible sur la page PyPI** de la 0.1.1 (condition nécessaire pour le registre, cf. §5).
- `mcp-publisher validate server.json` → ✅ validé par le registre officiel.

---

## 3. Architecture — le chemin des données

```
Garmin Connect API
      ↓  adapters/garmin_connect.py   (lib garminconnect, appels sync dans asyncio.to_thread
      ↓                                + lock process-croisé fcntl sur les tokens)
      ↓  health/service.py            (filtrage de champs — les consommateurs passent TOUJOURS par ici,
      ↓                                jamais par l'adapter directement)
      ↓  sync/engine.py               (staleness, backoff 429, curseur resumable, capability probes)
      ↓  store/store.py               (SQLite par compte, WAL)
      ↓  analytics/*.py               (PMC, ACWR, monotony, baselines, alerts, matcher, fusion)
      ↓  render/renderer.py           (verdict grammar, budgets tokens, drop order, safety banner)
      ↓  mcp_server/tools/*.py        (8 outils)
      ↓  mcp_server/server.py         (FastMCP stdio)
```

**Modules par taille** (indicatif de la densité de logique) : `sync/engine.py` (1059 l.) > `tools/activity.py` (670) > `tools/brief.py` (479) > `analytics/fusion.py` (399) > `store/store.py` (361).

Points d'entrée utiles :
- `mcp_server/context.py` — le `ToolContext`, seam entre les outils et le store/l'API. C'est ici que passe `ensure_ready()` (cold start automatique).
- `render/renderer.py` — tout ce qui est mise en forme et budget tokens. Ne pas formater dans les outils.
- `analytics/fusion.py` — la fusion readiness (le cœur de `garmin_brief`).

### Les 8 outils et leurs plafonds tokens

| Outil | Rôle | Cap |
|---|---|---|
| `garmin_brief` | "Je peux m'entraîner dur aujourd'hui ?" — verdict GREEN/AMBER/RED | 600 |
| `garmin_activities` | Parcourir le log, récupérer les IDs | 1 300 |
| `garmin_activity` | Une séance en profondeur (reps, fade, comparaison) | 1 000–4 000 |
| `garmin_athlete` | Carte de référence : zones, PRs, objectif, couverture | 600 |
| `garmin_set_profile` | Objectif de course / phase / disponibilité (local) | 200 |
| `garmin_log` | RPE, maladie, blessure — l'athlète prime sur les capteurs | 120 |
| `garmin_sync` | Refresh forcé / backfill | 150 |
| `garmin_raw` | Échappatoire borné vers les sources brutes | 5 000 |

---

## 4. Commandes

```bash
uv sync                          # install (dev inclus)
uv run pytest -q                 # 430 tests, ~1.5 s
uv run ruff check fartlek/ tests/
uv run fartlek auth              # login Garmin (email/password + MFA)
uv run fartlek doctor            # health check
uv run fartlek-mcp               # serveur MCP en stdio
```

Smoke check MCP autonome (JSON-RPC direct, sans client) :

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  | (cat; sleep 2) | uv run fartlek-mcp 2>/dev/null
```

**Test live** : un compte Garmin réel est utilisable via `GARMINTOKENS` / le scratchpad de session. Rester poli sur le volume d'appels (séquentiel, backoff sur 429).

---

## 5. Publication — comment ça marche, et ce qui reste

### Releases PyPI : automatiques

Le workflow `.github/workflows/release.yml` publie via **trusted publishing OIDC** — aucun token nulle part. Pour sortir une version :

```bash
# 1. bump "version" dans pyproject.toml
uv sync                                  # répercute dans uv.lock (ne pas oublier)
git commit -am "dist: bump to X.Y.Z"
git tag vX.Y.Z && git push origin main vX.Y.Z
```

Le tag déclenche : `ruff` → `pytest` → `uv build` → `uv publish`. Le job échoue avant l'upload si les tests cassent.

⚠️ **Une version PyPI est immuable.** Pas d'écrasement possible : pour corriger, on bumpe. (C'est exactement pourquoi la 0.1.0 a dû être suivie d'une 0.1.1, cf. §7.)

### Registre MCP officiel : une étape manuelle reste

Prérequis déjà remplis :
- `server.json` à la racine, **validé** (`mcp-publisher validate server.json`).
- Le token d'ownership `<!-- mcp-name: io.github.matisdsp/fartlek -->` est en tête du `README.md`, donc **publié sur la page PyPI** de la 0.1.1. C'est **le** mécanisme par lequel le registre vérifie qu'on possède bien le package PyPI (pas de champ de métadonnée structuré pour PyPI, contrairement à npm).
- CLI installé : `brew install mcp-publisher` (v1.8.0).

Il reste :

```bash
mcp-publisher login github      # ⚠️ OAuth interactif — nécessite un humain
mcp-publisher publish
```

`login github` ouvre un flux navigateur, **un agent ne peut pas l'exécuter seul**. Alternative automatisable : `mcp-publisher login github-oidc` dans un workflow GitHub Actions avec `permissions: id-token: write` — à faire si on veut que le registre se mette à jour à chaque release.

Namespace : `io.github.matisdsp/fartlek` (dérivé du compte GitHub, obtenu automatiquement par l'auth GitHub).

### Annuaires tiers (visibilité)

Le registre officiel est une base de métadonnées que les annuaires **scrapent** ; il n'y a pas de push. Pour la visibilité réelle, soumettre séparément, par ordre d'utilité : **Glama** (glama.ai/mcp), **mcp.so**, **PulseMCP**, puis une PR sur `punkpeye/awesome-mcp-servers`. Tous par formulaire web → nécessitent un humain.

---

## 6. Contrats à ne pas casser

Ce sont des invariants de conception, pas des préférences de style.

1. **Les formules sont des contrats.** Constantes PMC, EWMA de l'ACWR, scaling MAD `1.4826`, monotony de Foster : implémentées exactement comme spécifié dans `docs/DESIGN.md` §3, et testées contre des valeurs connues. Ne pas "améliorer" sans mettre à jour la spec.
2. **stdout est réservé au JSON-RPC.** Tout log va sur stderr. Un `print()` égaré casse le protocole pour tous les clients.
3. **Budgets tokens.** Chaque outil a un plafond dur appliqué par le renderer, avec troncature *annoncée*. Le contrat publié dans le README : tous les outils appelés une fois < 9K tokens (< 4K hors `garmin_raw`).
4. **L'athlète prime sur les capteurs.** Une maladie/douleur déclarée via `garmin_log` doit faire baisser un verdict GREEN. Jamais l'inverse.
5. **Ne rien fabriquer.** Si l'appareil ne produit pas une métrique, on l'annonce comme absente (cf. la section "Data coverage" de `garmin_athlete`). Pas de valeur inventée, pas de delta sur 8 semaines quand l'historique n'en couvre que 3.
6. **Jamais de secrets commités** (`~/.fartlek/`, `garmin_tokens.json`).
7. **Discipline de commit** : un changement cohérent = un commit, message impératif scopé (`analytics: add PMC engine`). Vérifier avant de committer.

---

## 7. Pièges rencontrés (vécus, pas théoriques)

- **`easy_ceiling` après cold start** : le plafond FC facile se calcule sur le max HR des **activités** (90 j), pas sur la série quotidienne `max_hr`. Au démarrage à froid la table `days` ne contient que quelques jours, potentiellement faciles → 80 % d'un max de jour facile donne un conseil dangereusement bas. Corrigé dans `analytics/fusion.py`, ne pas régresser.
- **Format de durée** : `5h00` et non `5:00` au-delà de l'heure — `5:00` se lit "cinq minutes". Les charges (`load`) s'affichent arrondies à l'entier, jamais en `%g` (qui produisait `228.385`).
- **Ordre des sections dans `pyproject.toml`** : `[project.urls]` placé avant `dependencies` fait échouer le build avec une erreur trompeuse (`URL 'dependencies' must be a string`) — en TOML, tout ce qui suit un en-tête de table lui appartient. Les clés du `[project]` doivent précéder toute sous-table.
- **`description` du `server.json` ≤ 100 caractères** — contrainte du registre non documentée dans le schéma. Le CLI ne le dit qu'à la validation (422).
- **Ownership PyPI pour le registre MCP** : le token `mcp-name:` doit être dans le README **au moment du build**, car c'est le README embarqué dans les métadonnées du package qui s'affiche sur PyPI. L'ajouter après publication ne sert à rien → il faut une nouvelle version.
- **`uv.lock` suit le bump de version** : `uv sync` après avoir changé `version` dans `pyproject.toml`, sinon le lockfile dérive (le CI utilise `--frozen`).

---

## 8. La suite — Phase 2 (v0.2, release phare)

Contenu détaillé dans `ROADMAP.md` et `docs/DESIGN.md` §6. En résumé :

**6 nouveaux outils** : `garmin_whats_changed`, `garmin_week`, `garmin_load`, `garmin_fitness` (projection de course + fenêtre d'affûtage), `garmin_recovery`, `garmin_reference` (glossaire des métriques).

**Moteur** : backfill Tier-2 (timeline de sommeil + SRI, EF/decoupling/durability, precedent mining rétroactif), mapping TID, triangulation de prédiction de course (Garmin/Tanda/Riegel), significativité des tendances (Hamed–Rao + SWC par métrique), audit de convergence de surentraînement, règles d'attribution.

**Qualité** : harnais d'évaluation (~30 tâches multi-outils, gates de régression tokens/appels), validation du moteur contre des exports intervals.icu, tuning des seuils de l'anomaly scanner sur données réelles multi-mois.

**Reliquat de Phase 0** : la CI gate de budget tokens avec un vrai tokenizer n'est pas encore en place — elle attend les golden renders de Phase 2.

### Questions ouvertes qui bloqueront la Phase 2

Listées en entier dans `docs/DESIGN.md` §7. Les plus susceptibles de coûter du temps :
- L'endpoint RHR `userstats-service` accepte-t-il des fenêtres arbitraires sur tous les types de compte ?
- Fenêtre maximale d'un appel body-battery (à sonder puis chunker).
- Disponibilité réelle des endpoints d'historique threshold-pace / race-prediction selon la génération d'appareil.
- Taux de faux positifs de l'anomaly scanner — à calibrer sur le compte réel du mainteneur avant v0.2 : sur-déclencher détruit la confiance autant que sous-déclencher.

---

## 9. Références

- `docs/DESIGN.md` — spec faisant autorité (§2 surface d'outils, §3 moteur de métriques, §5 conventions de format, §6 roadmap, §7 questions ouvertes).
- `ROADMAP.md` — plan par phase avec cases cochées.
- `CLAUDE.md` — instructions projet (architecture, commandes, discipline de commit).
- Repo : https://github.com/matisdsp/fartlek · PyPI : https://pypi.org/project/fartlek-mcp/
- `garth` est **déprécié** (Garmin a cassé son login en 2026) — tout passe par `garminconnect`, dont le source dans `.venv/.../garminconnect/` fait foi pour les endpoints.

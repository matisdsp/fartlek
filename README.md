# AI Coach SaaS — Prototype

Coach IA personnel avec accès aux données Garmin réelles de l'utilisateur via tool use.

## Architecture

Modular monolith avec DDD-lite — un dossier par bounded context.

```
src/
├── health/         # bounded context — données Garmin (port + adapter garth)
├── coaching/       # bounded context — chat + LLM (port + adapter Anthropic)
└── mcp_server/     # serveur MCP exposant les 12 outils Garmin à un client externe
```

---

## Deux façons d'utiliser le coach

### Option A — Via Claude Code + MCP (recommandé pour démarrer)

Aucune clé API Anthropic nécessaire — Claude Code (déjà installé localement) sert de LLM et appelle nos 12 outils Garmin via MCP.

**Setup :**

```bash
# 1. Install (une fois)
uv sync

# 2. Se connecter à Garmin (une fois — email/mot de passe + MFA éventuel)
uv run ai-coach-login
# → tokens stockés dans ~/.garminconnect/garmin_tokens.json

# 3. Lancer Claude Code depuis ce dossier
claude
```

Claude Code détecte `.mcp.json` à la racine et lance automatiquement le serveur `ai-coach-garmin` en sous-processus. Tu peux ensuite chatter normalement :

> "Comment était mon sommeil cette nuit ?"
> "Analyse ma dernière course à pied"
> "Je peux taper fort aujourd'hui ou je dois récupérer ?"
> "Fais-moi un plan d'entraînement running pour les 4 prochaines semaines en fonction de mon niveau actuel"

**Test rapide du serveur MCP en standalone :**

```bash
# Lister les 12 outils exposés
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  | (cat; sleep 2) | uv run ai-coach-mcp 2>/dev/null
```

### Option B — Via l'API FastAPI + UI HTML (nécessite clé Anthropic)

Pour avoir l'UI chat custom dans le navigateur (avec ta propre clé API Anthropic).

```bash
cp .env.example .env
# Remplir ANTHROPIC_API_KEY dans .env
uv run uvicorn src.main:app --reload --port 8000
open http://localhost:8000
```

---

## 12 outils Garmin exposés

| Outil | Description |
|---|---|
| `get_daily_health` | Pas, calories, FC repos/min/max, stress, body battery, intensité, étages, SpO2 |
| `get_sleep` | Score, qualité, durée, phases (deep/light/REM/awake), respiration |
| `get_recent_activities` | Liste des activités récentes (type, durée, FC, vitesse, training effect) |
| `get_activity_details` | Détail d'une activité (zones FC, splits, puissance, TSS, cadence) |
| `get_training_readiness` | Score 0-100, niveau, facteurs (sommeil/récup/HRV/charge/stress) |
| `get_training_status` | Statut (productive/peaking/recovery/...), VO2max, charge aiguë/chronique |
| `get_hrv` | HRV nuit dernière, moyenne 7j, statut, baseline |
| `get_body_battery` | Évolution énergie corporelle sur N jours |
| `get_stress` | Stress journalier détaillé (réparti par niveaux) |
| `get_user_profile` | Profil (âge, poids, VO2max, FTP) |
| `get_morning_readiness` | Score morning check-in |
| `get_personal_records` | Records personnels (5k, 10k, marathon, FTP...) |

---

## Stack

- **Backend** : FastAPI + Python 3.12 (async)
- **LLM** : Anthropic Claude (option B) ou Claude Code via MCP (option A)
- **Garmin** : `garminconnect` (login intégré `ai-coach-login`, tokens dans `~/.garminconnect/`)
- **Frontend** : HTML + Tailwind CDN + vanilla JS (option B)

## Limites prototype

- Pas d'auth utilisateur (mono-tenant local)
- Pas de DB (conversation en mémoire)
- Pas de streaming SSE
- Auth Garmin = tokens locaux créés par `uv run ai-coach-login` (auto-refresh)

## Roadmap

Voir le plan complet. Priorités phase 2 :
1. Persistence conversations (SQLite + Alembic)
2. Contexte `users/` (auth + multi-tenant)
3. Contexte `training/` (plans d'entraînement structurés + tracking progression)

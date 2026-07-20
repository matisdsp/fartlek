"""MCP server — exposes Garmin tools to Claude Code (or any MCP client).

Same 12 use cases as the FastAPI chat endpoint, just behind the MCP protocol
instead of an Anthropic-format tool registry. Both consumers call HealthService
— the application service is the single entry point for the health context.

Run via stdio (Claude Code spawns the subprocess and pipes JSON-RPC over
stdin/stdout). Environment variables read at startup:
  GARMINTOKENS : path to Garmin tokens (default: ~/.fartlek/tokens),
                 created by `fartlek auth`
  FARTLEK_HOME : base data directory (default: ~/.fartlek)
"""
from __future__ import annotations

import logging
import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from fartlek.health.adapters.garmin_connect import GarminConnectAdapter
from fartlek.health.service import HealthService
from fartlek.paths import default_tokenstore

# Log to stderr — stdout is reserved for the MCP JSON-RPC channel
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "WARNING"),
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
log = logging.getLogger("fartlek-mcp")

_tokenstore = default_tokenstore()
_adapter = GarminConnectAdapter(tokenstore=_tokenstore)
_health = HealthService(garmin=_adapter)

mcp = FastMCP("fartlek")


@mcp.tool(
    description=(
        "Récupère les métriques journalières Garmin de l'utilisateur : pas, calories, "
        "distance, fréquence cardiaque (repos/min/max/moyenne), stress moyen, body battery, "
        "minutes d'intensité, étages, SpO2. Date facultative (défaut : aujourd'hui)."
    ),
)
async def get_daily_health(date: str | None = None) -> dict[str, Any]:
    return await _health.get_daily_health(date)


@mcp.tool(
    description=(
        "Données de sommeil pour une nuit : score, qualité, durée totale, phases "
        "(deep/light/REM/awake), heures de coucher et lever, SpO2 moyen, respiration. "
        "`date` = date du réveil (YYYY-MM-DD)."
    ),
)
async def get_sleep(date: str | None = None) -> dict[str, Any]:
    return await _health.get_sleep(date)


@mcp.tool(
    description=(
        "Liste des activités sportives récentes (running, vélo, natation, muscu, etc.) "
        "avec type, durée, distance, FC moyenne/max, vitesse, calories, effet "
        "d'entraînement aérobie/anaérobie. Utilise `activity_id` pour drill-down avec "
        "get_activity_details."
    ),
)
async def get_recent_activities(days_back: int = 14, limit: int = 20) -> list[dict[str, Any]]:
    return await _health.get_recent_activities(days_back=days_back, limit=limit)


@mcp.tool(
    description=(
        "Détails complets d'une activité : zones FC, splits, puissance moyenne/max/normalisée, "
        "TSS, IF, cadence, oscillation verticale, GCT, training effect détaillé."
    ),
)
async def get_activity_details(activity_id: str) -> dict[str, Any]:
    return await _health.get_activity_details(activity_id)


@mcp.tool(
    description=(
        "Score de préparation à l'entraînement (0-100) : niveau, feedback, facteurs "
        "(sommeil, récupération, HRV, charge aiguë, stress). Utile pour décider si tu "
        "peux taper fort ou si tu dois récupérer."
    ),
)
async def get_training_readiness(date: str | None = None) -> dict[str, Any]:
    return await _health.get_training_readiness(date)


@mcp.tool(
    description=(
        "Statut d'entraînement agrégé (productive, maintaining, peaking, detraining, "
        "recovery, unproductive) avec VO2max, fitness age, charge aiguë/chronique, "
        "load focus (anaérobie, aérobie haute, aérobie basse)."
    ),
)
async def get_training_status(date: str | None = None) -> dict[str, Any]:
    return await _health.get_training_status(date)


@mcp.tool(
    description=(
        "Variabilité cardiaque (HRV) : moyenne nuit dernière, moyenne 7 jours, statut "
        "(balanced/unbalanced/low), baseline personnel, feedback."
    ),
)
async def get_hrv(date: str | None = None) -> dict[str, Any]:
    return await _health.get_hrv(date)


@mcp.tool(
    description=(
        "Évolution du body battery sur une plage de dates : valeur quotidienne "
        "chargée/déchargée, max/min."
    ),
)
async def get_body_battery(
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[dict[str, Any]]:
    return await _health.get_body_battery(start_date=start_date, end_date=end_date)


@mcp.tool(
    description=(
        "Stress journalier détaillé : niveau max et moyen, durée totale + répartition "
        "(repos / faible / moyen / élevé / activité)."
    ),
)
async def get_stress(date: str | None = None) -> dict[str, Any]:
    return await _health.get_stress(date)


@mcp.tool(
    description=(
        "Profil utilisateur Garmin : nom, âge, sexe, poids, taille, VO2max running/cycling, "
        "seuil lactique, FTP estimée, statut d'entraînement."
    ),
)
async def get_user_profile() -> dict[str, Any]:
    return await _health.get_user_profile()


@mcp.tool(
    description="Morning readiness check-in (si disponible sur la montre).",
)
async def get_morning_readiness(date: str | None = None) -> dict[str, Any]:
    return await _health.get_morning_readiness(date)


@mcp.tool(
    description="Records personnels (PRs) : meilleur 5k/10k/marathon, FTP, etc.",
)
async def get_personal_records() -> list[dict[str, Any]]:
    return await _health.get_personal_records()


def main() -> None:
    log.info("Starting AI Coach MCP server (tokenstore=%s)", _tokenstore)
    mcp.run()


if __name__ == "__main__":
    main()

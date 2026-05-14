"""ToolRegistry — schemas exposed to Claude + dispatcher to execute tool calls.

Each entry pairs an Anthropic-format tool schema with a handler function.
Handlers all share signature (health_service, args) -> result.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from src.coaching.adapters.tools import health_tools
from src.coaching.exceptions import ToolExecutionError
from src.health.service import HealthService

log = logging.getLogger(__name__)

ToolHandler = Callable[[HealthService, dict[str, Any]], Awaitable[Any]]

_DATE_PROP = {
    "type": "string",
    "description": "Date au format YYYY-MM-DD. Si omise, aujourd'hui est utilisé.",
}


def _build_schemas() -> list[dict[str, Any]]:
    return [
        {
            "name": "get_daily_health",
            "description": "Récupère les métriques journalières de l'utilisateur : pas, calories, distance, fréquence cardiaque (repos/min/max/moyenne), stress moyen, body battery, minutes d'intensité, étages, SpO2.",
            "input_schema": {
                "type": "object",
                "properties": {"date": _DATE_PROP},
                "required": [],
            },
        },
        {
            "name": "get_sleep",
            "description": "Récupère les données de sommeil pour une nuit donnée : score, qualité, durée totale, phases (deep/light/REM/awake), heures de coucher et lever, SpO2 moyen, respiration.",
            "input_schema": {
                "type": "object",
                "properties": {"date": {**_DATE_PROP, "description": "Date du réveil (YYYY-MM-DD)."}},
                "required": [],
            },
        },
        {
            "name": "get_recent_activities",
            "description": "Liste les activités sportives récentes (courses, cyclisme, natation, muscu, etc.) avec type, durée, distance, FC moyenne/max, vitesse, calories, effet d'entraînement (aérobie/anaérobie), dénivelé. Utiliser activity_id pour drill-down avec get_activity_details.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "days_back": {"type": "integer", "description": "Nombre de jours en arrière (défaut 14).", "default": 14},
                    "limit": {"type": "integer", "description": "Nombre max d'activités à retourner (défaut 20).", "default": 20},
                },
                "required": [],
            },
        },
        {
            "name": "get_activity_details",
            "description": "Détails complets d'une activité spécifique : zones FC, splits, puissance moyenne/max/normalisée, TSS, IF, cadence, oscillation verticale, GCT, training effect détaillé. Récupérer activity_id via get_recent_activities.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "activity_id": {"type": "string", "description": "ID de l'activité Garmin."},
                },
                "required": ["activity_id"],
            },
        },
        {
            "name": "get_training_readiness",
            "description": "Score de préparation à l'entraînement (0-100) du jour : niveau, feedback, facteurs (sommeil, récupération, HRV, charge aiguë, stress). Utile pour décider si on peut taper fort ou si on doit récupérer.",
            "input_schema": {
                "type": "object",
                "properties": {"date": _DATE_PROP},
                "required": [],
            },
        },
        {
            "name": "get_training_status",
            "description": "Statut d'entraînement agrégé (productive, maintaining, peaking, detraining, recovery, unproductive) avec VO2max, fitness age, charge aiguë/chronique, répartition load focus (anaérobie / aérobie haute / aérobie basse).",
            "input_schema": {
                "type": "object",
                "properties": {"date": _DATE_PROP},
                "required": [],
            },
        },
        {
            "name": "get_hrv",
            "description": "Variabilité cardiaque (HRV) pour une date : moyenne nuit dernière, moyenne 7 jours, statut (balanced/unbalanced/low), baseline personnel, feedback.",
            "input_schema": {
                "type": "object",
                "properties": {"date": _DATE_PROP},
                "required": [],
            },
        },
        {
            "name": "get_body_battery",
            "description": "Évolution du body battery (énergie corporelle) sur une plage de dates : valeur quotidienne chargée/déchargée, max/min.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "start_date": {"type": "string", "description": "Début (YYYY-MM-DD). Si omis, end_date - 7 jours."},
                    "end_date": {"type": "string", "description": "Fin (YYYY-MM-DD). Si omise, aujourd'hui."},
                },
                "required": [],
            },
        },
        {
            "name": "get_stress",
            "description": "Stress journalier détaillé : niveau max et moyen, durée totale + répartition (repos/faible/moyen/élevé/activité).",
            "input_schema": {
                "type": "object",
                "properties": {"date": _DATE_PROP},
                "required": [],
            },
        },
        {
            "name": "get_user_profile",
            "description": "Profil utilisateur : nom, âge, sexe, poids, taille, VO2max running/cycling, seuil lactique, FTP estimée, statut d'entraînement.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "get_morning_readiness",
            "description": "Score morning readiness check-in (si disponible sur la montre de l'utilisateur).",
            "input_schema": {
                "type": "object",
                "properties": {"date": _DATE_PROP},
                "required": [],
            },
        },
        {
            "name": "get_personal_records",
            "description": "Records personnels de l'utilisateur (PRs) : meilleur 5k/10k/marathon, FTP, etc.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
    ]


def _build_handlers() -> dict[str, ToolHandler]:
    return {
        "get_daily_health": health_tools.get_daily_health,
        "get_sleep": health_tools.get_sleep,
        "get_recent_activities": health_tools.get_recent_activities,
        "get_activity_details": health_tools.get_activity_details,
        "get_training_readiness": health_tools.get_training_readiness,
        "get_training_status": health_tools.get_training_status,
        "get_hrv": health_tools.get_hrv,
        "get_body_battery": health_tools.get_body_battery,
        "get_stress": health_tools.get_stress,
        "get_user_profile": health_tools.get_user_profile,
        "get_morning_readiness": health_tools.get_morning_readiness,
        "get_personal_records": health_tools.get_personal_records,
    }


class ToolRegistry:
    def __init__(self, health: HealthService):
        self._health = health
        self._schemas = _build_schemas()
        self._handlers = _build_handlers()
        # Sanity check: every schema has a handler
        missing = {s["name"] for s in self._schemas} - set(self._handlers)
        if missing:
            raise RuntimeError(f"Tool schemas without handlers: {missing}")

    def schemas(self) -> list[dict[str, Any]]:
        return self._schemas

    async def execute(self, name: str, tool_input: dict[str, Any]) -> tuple[str, bool]:
        """Run a tool by name. Returns (json_result, is_error)."""
        handler = self._handlers.get(name)
        if handler is None:
            return json.dumps({"error": f"Unknown tool: {name}"}), True

        try:
            result = await handler(self._health, tool_input)
        except Exception as exc:
            log.exception("Tool %s failed", name)
            return json.dumps({"error": str(exc), "tool": name}), True

        try:
            payload = json.dumps(result, ensure_ascii=False, default=str)
        except (TypeError, ValueError) as exc:
            raise ToolExecutionError(name, f"Result not JSON-serializable: {exc}") from exc
        return payload, False

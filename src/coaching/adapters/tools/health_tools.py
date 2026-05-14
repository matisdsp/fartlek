"""Health tools exposed to Claude.

Each function:
  - takes the parsed tool_input dict
  - calls HealthService (never GarminPort directly — the wall stays)
  - returns the result; ToolRegistry serializes to JSON for the LLM

Adding a new context's tools (training, nutrition) means a sibling file
that calls that context's service. Don't reach across into other adapters.
"""
from __future__ import annotations

from typing import Any

from src.health.service import HealthService


async def get_daily_health(health: HealthService, args: dict[str, Any]) -> Any:
    return await health.get_daily_health(args.get("date"))


async def get_sleep(health: HealthService, args: dict[str, Any]) -> Any:
    return await health.get_sleep(args.get("date"))


async def get_recent_activities(health: HealthService, args: dict[str, Any]) -> Any:
    return await health.get_recent_activities(
        days_back=int(args.get("days_back", 14)),
        limit=int(args.get("limit", 20)),
    )


async def get_activity_details(health: HealthService, args: dict[str, Any]) -> Any:
    return await health.get_activity_details(str(args["activity_id"]))


async def get_training_readiness(health: HealthService, args: dict[str, Any]) -> Any:
    return await health.get_training_readiness(args.get("date"))


async def get_training_status(health: HealthService, args: dict[str, Any]) -> Any:
    return await health.get_training_status(args.get("date"))


async def get_hrv(health: HealthService, args: dict[str, Any]) -> Any:
    return await health.get_hrv(args.get("date"))


async def get_body_battery(health: HealthService, args: dict[str, Any]) -> Any:
    return await health.get_body_battery(
        start_date=args.get("start_date"),
        end_date=args.get("end_date"),
    )


async def get_stress(health: HealthService, args: dict[str, Any]) -> Any:
    return await health.get_stress(args.get("date"))


async def get_user_profile(health: HealthService, args: dict[str, Any]) -> Any:
    return await health.get_user_profile()


async def get_morning_readiness(health: HealthService, args: dict[str, Any]) -> Any:
    return await health.get_morning_readiness(args.get("date"))


async def get_personal_records(health: HealthService, args: dict[str, Any]) -> Any:
    return await health.get_personal_records()

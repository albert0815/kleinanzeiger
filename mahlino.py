"""Client for the Mahlino REST API (meal plan and recipes).

Results keep the shape the bot uses internally: a plan is a list of
{"date", "recipes": [{"id", "name", "duration"}]}, where "id" is the recipe slug.
Only lunch entries are considered.
"""

from __future__ import annotations

import datetime
import os

import httpx

MAHLINO_URL = os.environ.get("MAHLINO_URL", "http://ubuntu.fritz.box:8000").rstrip("/")
MAHLINO_API_KEY = os.environ.get("MAHLINO_API_KEY", "")
_TIMEOUT = 15
_MEAL_TYPE = "lunch"
_MAX_DAYS_PER_REQUEST = 60


def _headers() -> dict[str, str]:
    return {"X-API-Key": MAHLINO_API_KEY, "Accept": "application/json"}


def _plan_requests(start: datetime.date, end: datetime.date) -> list[dict]:
    """Query params covering start..end; the API returns at most 60 days per call."""
    params = []
    while start <= end:
        days = min((end - start).days + 1, _MAX_DAYS_PER_REQUEST)
        params.append({"start": start.isoformat(), "days": days})
        start += datetime.timedelta(days=days)
    return params


def _parse_plan(data: dict) -> list[dict]:
    plan = []
    for day in data.get("days", []):
        recipes = []
        for entry in day.get("entries", []):
            if entry.get("meal_type") != _MEAL_TYPE:
                continue
            recipe = entry.get("recipe")
            if recipe:
                minutes = (recipe.get("prep_minutes") or 0) + (
                    recipe.get("cook_minutes") or 0
                )
                recipes.append(
                    {
                        "id": recipe.get("slug"),
                        "name": recipe.get("title", "?"),
                        "duration": minutes or "",
                    }
                )
            elif entry.get("note"):
                # Free-text entry without a recipe, e.g. "Reste"
                recipes.append({"id": None, "name": entry["note"], "duration": ""})
        plan.append({"date": day.get("day"), "recipes": recipes})
    return plan


async def fetch_plan(start: datetime.date, end: datetime.date) -> list[dict]:
    plan: list[dict] = []
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        for params in _plan_requests(start, end):
            resp = await client.get(
                f"{MAHLINO_URL}/api/v1/meal-plan", params=params, headers=_headers()
            )
            resp.raise_for_status()
            plan.extend(_parse_plan(resp.json()))
    return plan


def fetch_plan_sync(start: datetime.date, end: datetime.date) -> list[dict]:
    plan: list[dict] = []
    with httpx.Client(timeout=_TIMEOUT) as client:
        for params in _plan_requests(start, end):
            resp = client.get(
                f"{MAHLINO_URL}/api/v1/meal-plan", params=params, headers=_headers()
            )
            resp.raise_for_status()
            plan.extend(_parse_plan(resp.json()))
    return plan


def fetch_recipes_sync() -> list[dict]:
    resp = httpx.get(
        f"{MAHLINO_URL}/api/v1/recipes/planning", headers=_headers(), timeout=_TIMEOUT
    )
    resp.raise_for_status()
    return [
        {"id": r["slug"], "name": r["title"], "lastPlanDate": r.get("last_cooked")}
        for r in resp.json()
    ]


def save_lunch_entry_sync(day: str, slug: str) -> None:
    resp = httpx.post(
        f"{MAHLINO_URL}/api/v1/meal-plan/entries",
        json={"day": day, "meal_type": _MEAL_TYPE, "recipe": slug},
        headers=_headers(),
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()

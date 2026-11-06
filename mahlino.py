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
    """All active recipes with their total time, which planning needs."""
    resp = httpx.get(
        f"{MAHLINO_URL}/api/v1/recipes",
        params={"archived": "false"},
        headers=_headers(),
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return [
        {
            "id": r["slug"],
            "name": r["title"],
            "lastPlanDate": r.get("last_cooked"),
            "minutes": ((r.get("prep_minutes") or 0) + (r.get("cook_minutes") or 0)) or None,
        }
        for r in resp.json()
    ]


def _post_entry(payload: dict) -> None:
    resp = httpx.post(
        f"{MAHLINO_URL}/api/v1/meal-plan/entries",
        json={"meal_type": _MEAL_TYPE, **payload},
        headers=_headers(),
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()


def save_lunch_entry_sync(day: str, slug: str) -> None:
    _post_entry({"day": day, "recipe": slug})


def save_lunch_note_sync(day: str, note: str) -> None:
    _post_entry({"day": day, "note": note})


def fetch_entry_ids_sync(day: datetime.date) -> list:
    """IDs of the lunch entries of a day."""
    resp = httpx.get(
        f"{MAHLINO_URL}/api/v1/meal-plan",
        params={"start": day.isoformat(), "days": 1},
        headers=_headers(),
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return [
        entry["id"]
        for found in resp.json().get("days", [])
        for entry in found.get("entries", [])
        if entry.get("meal_type") == _MEAL_TYPE
    ]


def delete_entry_sync(entry_id) -> None:
    resp = httpx.delete(
        f"{MAHLINO_URL}/api/v1/meal-plan/entries/{entry_id}",
        headers=_headers(),
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()


def add_shopping_item_sync(text: str, quantity: float | None, unit: str | None) -> None:
    payload = {"text": text, "quantity": quantity, "unit": unit}
    resp = httpx.post(
        f"{MAHLINO_URL}/api/v1/shopping-list/items",
        json={k: v for k, v in payload.items() if v is not None},
        headers=_headers(),
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()


def add_days_to_shopping_list_sync(days: list[str]) -> None:
    resp = httpx.post(
        f"{MAHLINO_URL}/api/v1/meal-plan/to-shopping-list",
        json={"days": days, "meal_types": [_MEAL_TYPE]},
        headers=_headers(),
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()


def set_recipe_image_from_url_sync(slug: str, url: str) -> None:
    """Mahlino downloads the photo itself and checks that it is a public address."""
    resp = httpx.put(
        f"{MAHLINO_URL}/api/v1/recipes/{slug}/image/from-url",
        json={"url": url},
        headers=_headers(),
        timeout=30,
    )
    resp.raise_for_status()


def create_recipe_sync(recipe: dict) -> dict:
    resp = httpx.post(
        f"{MAHLINO_URL}/api/v1/recipes",
        json=recipe,
        headers=_headers(),
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()

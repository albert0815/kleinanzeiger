"""Household lunch proposals for Mahlino: validate, store, confirm, save.

The agent can only propose. Saving happens in apply_proposal, triggered by a
Telegram button, so nothing reaches Mahlino without a person confirming it.
"""

from __future__ import annotations

import datetime
import json
import logging
import threading
import uuid
from pathlib import Path

import ad_store
import mahlino

log = logging.getLogger("kleinanzeigen-agent.lunch")

NO_COOKING_WEEKDAY = 1  # Tuesday: someone else cooks, so nothing needs planning.
PROPOSAL_COOLDOWN = datetime.timedelta(days=7)
RECENT_DAYS = (14, 7, 0)
MIN_CANDIDATES = 15
_LOCK = threading.Lock()


def _path() -> Path:
    return ad_store.base_dir() / ".lunch-proposal.json"


def load_proposal() -> dict | None:
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        log.error("Cannot read lunch proposal: %s", e)
        return None


def _save(proposal: dict) -> None:
    temporary = _path().with_suffix(".tmp")
    temporary.write_text(json.dumps(proposal, ensure_ascii=False, indent=1), encoding="utf-8")
    temporary.replace(_path())


def is_no_cooking_day(day: datetime.date) -> bool:
    return day.weekday() == NO_COOKING_WEEKDAY


def should_propose(
    plan: list[dict], tomorrow: datetime.date, now: datetime.datetime | None = None
) -> bool:
    """One automatic proposal per week, only if tomorrow really needs a meal."""
    if is_no_cooking_day(tomorrow):
        return False
    entry = next((e for e in plan if e["date"] == tomorrow.isoformat()), None)
    if entry and entry["recipes"]:
        return False
    proposal = load_proposal()
    if not proposal:
        return True
    now = now or datetime.datetime.now(datetime.UTC)
    created = datetime.datetime.fromisoformat(proposal["created_at"])
    return now - created >= PROPOSAL_COOLDOWN


def planning_data(start: datetime.date, end: datetime.date, today: datetime.date) -> dict:
    """Free days and suitable recipes, so the model need not filter the history."""
    plan = mahlino.fetch_plan_sync(today - datetime.timedelta(days=max(RECENT_DAYS)), end)
    by_date = {e["date"]: e["recipes"] for e in plan}
    free_days, skipped_days, planned = [], [], []
    day = start
    while day <= end:
        recipes = by_date.get(day.isoformat(), [])
        if recipes:
            planned.append({"date": day.isoformat(), "recipes": [r["name"] for r in recipes]})
        elif is_no_cooking_day(day):
            skipped_days.append(day.isoformat())
        else:
            free_days.append(day.isoformat())
        day += datetime.timedelta(days=1)

    recipes = mahlino.fetch_recipes_sync()
    used_on: dict[str, str] = {}
    for entry in plan:
        for recipe in entry["recipes"]:
            if recipe["id"]:
                used_on[recipe["id"]] = max(entry["date"], used_on.get(recipe["id"], ""))
    candidates: list[dict] = []
    for days in RECENT_DAYS:
        limit = (today - datetime.timedelta(days=days)).isoformat()
        candidates = [
            r
            for r in recipes
            if not days or (used_on.get(r["id"], "") < limit and (r["lastPlanDate"] or "") < limit)
        ]
        if len(candidates) >= MIN_CANDIDATES:
            break
    candidates.sort(key=lambda r: r["lastPlanDate"] or "")
    return {
        "free_days": free_days,
        "no_cooking_days": skipped_days,
        "planned_days": planned,
        "candidates": candidates,
    }


def validate_days(
    days: list[dict], today: datetime.date, recipes: list[dict], plan: list[dict]
) -> list[dict] | str:
    """Return normalized days, or a message describing what is wrong."""
    if not days:
        return "Der Vorschlag enthält keine Tage."
    names = {r["id"]: r["name"] for r in recipes}
    planned = {e["date"]: e["recipes"] for e in plan}
    normalized: list[dict] = []
    seen = set()
    for item in days:
        try:
            day = datetime.date.fromisoformat(str(item.get("date")))
        except ValueError:
            return f"Ungültiges Datum: {item.get('date')}"
        recipe_id, note = item.get("recipe_id"), item.get("note")
        replace = bool(item.get("replace"))
        if day < today:
            return f"{day} liegt in der Vergangenheit."
        if day in seen:
            return f"{day} kommt mehrfach vor."
        seen.add(day)
        if recipe_id and note:
            return f"{day}: Bitte entweder recipe_id oder note angeben."
        if recipe_id and recipe_id not in names:
            return f"{day}: Unbekanntes Rezept {recipe_id}."
        if recipe_id and is_no_cooking_day(day):
            return f"{day} ist ein Dienstag, dafür wird kein Rezept geplant."
        if not recipe_id and not note and not replace:
            return f"{day}: Es fehlt recipe_id oder note."
        if planned.get(day.isoformat()) and not replace:
            return f"{day} ist schon geplant. Nur mit replace=true ändern."
        normalized.append(
            {
                "date": day.isoformat(),
                "recipe_id": recipe_id,
                "name": names.get(recipe_id) if recipe_id else note,
                "note": note,
                "replace": replace,
                "previous": [r["name"] for r in planned.get(day.isoformat(), [])],
            }
        )
    return sorted(normalized, key=lambda d: d["date"])


def store_proposal(days: list[dict]) -> dict:
    proposal = {
        "id": uuid.uuid4().hex[:12],
        "created_at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        "status": "open",
        "announced": False,
        "days": days,
    }
    with _LOCK:
        _save(proposal)
    return proposal


def take_unannounced() -> dict | None:
    with _LOCK:
        proposal = load_proposal()
        if not proposal or proposal["status"] != "open" or proposal["announced"]:
            return None
        proposal["announced"] = True
        _save(proposal)
    return proposal


def _claim(proposal_id: str, status: str) -> dict | None:
    with _LOCK:
        proposal = load_proposal()
        if not proposal or proposal["id"] != proposal_id or proposal["status"] != "open":
            return None
        proposal["status"] = status
        _save(proposal)
    return proposal


def reject_proposal(proposal_id: str) -> bool:
    return _claim(proposal_id, "rejected") is not None


def apply_proposal(proposal_id: str, today: datetime.date) -> dict | None:
    """Save a proposal once. Returns None if it is gone or was already handled.

    A failed run can be confirmed again: days that were saved meanwhile count as
    planned and are skipped rather than added twice.
    """
    proposal = _claim(proposal_id, "saving")
    if not proposal:
        return None
    saved, skipped = [], []
    try:
        for item in proposal["days"]:
            day = datetime.date.fromisoformat(item["date"])
            if day < today:
                skipped.append(item)
                continue
            entries = mahlino.fetch_entry_ids_sync(day)
            if entries and not item["replace"]:
                skipped.append(item)
                continue
            for entry_id in entries:
                mahlino.delete_entry_sync(entry_id)
            if item["recipe_id"]:
                mahlino.save_lunch_entry_sync(item["date"], item["recipe_id"])
            elif item["note"]:
                mahlino.save_lunch_note_sync(item["date"], item["note"])
            # A saved replacement must not replace itself on a repeated confirmation.
            item["replace"] = False
            saved.append(item)
    except Exception:
        with _LOCK:
            proposal["status"] = "open"
            _save(proposal)
        raise
    with _LOCK:
        proposal["status"] = "saved"
        _save(proposal)
    return {"saved": saved, "skipped": skipped}

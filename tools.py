"""Strands @tool definitions for the agent.

All tools are synchronous so they work correctly when the Strands agent is
called via asyncio.to_thread() from the Telegram asyncio event loop.
The save_lunch_plan tool is gated: it requires agent.state["plan_confirmed"] == True,
which is set explicitly by the Telegram confirmation handler before re-invoking the agent.
"""

from __future__ import annotations

import contextvars
import datetime
import logging
import uuid
from zoneinfo import ZoneInfo

from strands import tool

import ad_store
import mahlino

log = logging.getLogger("kleinanzeigen-agent.tools")

# ---------------------------------------------------------------------------
# Per-call chat_id injection via ContextVar
# asyncio.to_thread copies the current context, so setting this before
# calling to_thread() makes it available inside the synchronous tools.
# ---------------------------------------------------------------------------
_chat_id_ctx: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "kleinanzeigen_chat_id", default=None
)


def set_agent_chat_id(chat_id: int) -> None:
    """Set the active chat_id for the current async task / thread context."""
    _chat_id_ctx.set(chat_id)


_TZ = ZoneInfo("Europe/Berlin")

# ---------------------------------------------------------------------------
# Date / time
# ---------------------------------------------------------------------------

@tool
def get_current_date() -> dict:
    """Return the current date and day-of-week in the Europe/Berlin timezone.

    Always call this tool first whenever the task involves dates (e.g. fetching
    or planning meals), so you know today's actual date rather than guessing.

    Returns a dict with:
    - date: ISO date string, e.g. "2026-05-11"
    - weekday: German weekday name, e.g. "Montag"
    - weekday_en: English weekday name, e.g. "Monday"
    """
    now = datetime.datetime.now(_TZ)
    weekdays_de = [
        "Montag", "Dienstag", "Mittwoch", "Donnerstag",
        "Freitag", "Samstag", "Sonntag",
    ]
    return {
        "date": now.date().isoformat(),
        "weekday": weekdays_de[now.weekday()],
        "weekday_en": now.strftime("%A"),
    }

@tool
def get_recipes() -> list[dict]:
    """Fetch all available (non-archived) recipes from Mahlino.

    Returns a slim list; each object contains:
    - id: unique recipe identifier (use this when saving a plan)
    - name: recipe name
    - lastPlanDate: ISO date string of when it was last cooked (or null)
    """
    return mahlino.fetch_recipes_sync()


@tool
def get_lunch_plan(start_date: str, end_date: str) -> list[dict]:
    """Fetch the lunch plan for a date range from Mahlino.

    Args:
        start_date: ISO date string, e.g. "2026-05-12"
        end_date:   ISO date string, e.g. "2026-05-18"

    Returns a slim list of day-entries. Each entry contains:
    - date: ISO date string
    - recipes: list of {id, name} for each recipe planned that day (may be empty)
    """
    return [
        {
            "date": entry["date"],
            "recipes": [
                {"id": r["id"], "name": r["name"]} for r in entry["recipes"]
            ],
        }
        for entry in mahlino.fetch_plan_sync(
            datetime.date.fromisoformat(start_date),
            datetime.date.fromisoformat(end_date),
        )
    ]


@tool
def save_lunch_plan(date: str, recipes: list[dict], **kwargs) -> str:
    """Save a lunch plan entry for a specific date to Mahlino.

    IMPORTANT: This tool must only be called after the user has explicitly confirmed
    the plan. The confirmation is tracked in agent.state["plan_confirmed"]. If
    confirmation is missing, this tool returns an error message instead of saving.

    Args:
        date:    ISO date string, e.g. "2026-05-12"
        recipes: list of recipe references – each must contain at least {"id": <id>}.

    Returns a success message or an error description.
    """
    # Check agent state for confirmation flag
    agent_context = kwargs.get("agent_context") or kwargs.get("context")
    if agent_context is not None:
        agent_state = getattr(agent_context, "state", None)
        if agent_state is not None and not agent_state.get("plan_confirmed", False):
            return (
                "Fehler: Der Plan wurde noch nicht vom Nutzer bestätigt. "
                "Bitte warte auf eine ausdrückliche Bestätigung (z.B. '✅ Annehmen'), "
                "bevor du den Plan speicherst."
            )

    slugs = []
    for ref in recipes:
        slug = ref.get("id")
        if slug is None:
            return f"Fehler: Rezept-Referenz ohne ID: {ref}"
        slugs.append(slug)

    for slug in slugs:
        mahlino.save_lunch_entry_sync(date, slug)

    log.info("Saved lunch plan for %s", date)
    return f"Plan für {date} erfolgreich gespeichert."


# ---------------------------------------------------------------------------
# Kleinanzeigen management tools
# ---------------------------------------------------------------------------

_queue_enqueue_fn = None
_request_confirmation_fn = None


def set_queue_enqueue_fn(fn) -> None:
    """Register the queue enqueue function from main.py."""
    global _queue_enqueue_fn
    _queue_enqueue_fn = fn


def set_confirmation_fn(fn) -> None:
    """Register fn(chat_id, action, ad) that asks the user via Telegram buttons."""
    global _request_confirmation_fn
    _request_confirmation_fn = fn


def _ask_confirmation(action: str, ad_key: str) -> str:
    chat_id = _chat_id_ctx.get()
    if chat_id is None:
        return "Fehler: Kein Nutzerkontext verfügbar."
    if _request_confirmation_fn is None:
        return "Fehler: Bestätigung nicht verfügbar."
    ad = ad_store.find_ad(chat_id, ad_key)
    if not ad:
        return f"Fehler: Kein Inserat mit Schlüssel {ad_key} gefunden."
    _request_confirmation_fn(chat_id, action, ad)
    return (
        f"Der Nutzer wurde per Button um Bestätigung für «{ad.title}» gebeten. "
        "Die Aktion ist noch nicht ausgeführt."
    )


@tool
def list_kleinanzeigen_ads() -> dict:
    """Show the ads that are on Kleinanzeigen, as of the last online check.

    Returns:
    - synced_at: UTC time of the last online check, null if there was none
    - health: result of the last online check; ok is false if it failed
    - ads: entries with key (use it for the other tools; null means not editable
      yet), title, state (active, reserved or inactive), expires, expiring_soon
      and auto_republish

    Prices are not available here. Never talk about local files.
    Always mention synced_at. Call refresh_kleinanzeigen_status if it is old.
    """
    chat_id = _chat_id_ctx.get()
    if chat_id is None:
        return {"error": "Kein Nutzerkontext verfügbar."}
    return ad_store.online_view(chat_id)


@tool
def refresh_kleinanzeigen_status() -> str:
    """Queue a fresh online check. The user is told about differences afterwards."""
    chat_id = _chat_id_ctx.get()
    if chat_id is None:
        return "Fehler: Kein Nutzerkontext verfügbar."
    if _queue_enqueue_fn is None:
        return "Fehler: Warteschlange nicht initialisiert."
    _queue_enqueue_fn(
        job_id=str(uuid.uuid4()),
        chat_id=chat_id,
        job_type="sync_ads",
        data={"chat_id": chat_id, "announce": True},
        max_retries=1,
    )
    return "Online-Abgleich wurde gestartet."


@tool
def search_kleinanzeigen_categories(query: str) -> list[str]:
    """Find valid category names. Use one of them exactly in the ad JSON.

    Args:
        query: words to search for, e.g. "Kinderschuhe"
    """
    return ad_store.search_categories(query)


@tool
def delete_kleinanzeigen_ad(ad_key: str) -> str:
    """Ask the user to confirm deleting an ad online. Nothing is deleted yet.

    Args:
        ad_key: the 'key' value returned by list_kleinanzeigen_ads()
    """
    return _ask_confirmation("delete", ad_key)


@tool
def mark_kleinanzeigen_ad_sold(ad_key: str) -> str:
    """Ask the user to confirm that an ad is sold. Nothing is changed yet.

    After confirmation it is removed from the site if needed and recorded as sold.

    Args:
        ad_key: the 'key' value returned by list_kleinanzeigen_ads()
    """
    return _ask_confirmation("sold", ad_key)


@tool
def set_kleinanzeigen_auto_republish(ad_key: str, enabled: bool) -> str:
    """Turn automatic republishing on or off for an ad. It stays online either way.

    Args:
        ad_key: the 'key' value returned by list_kleinanzeigen_ads()
        enabled: true to republish automatically, false to stop
    """
    chat_id = _chat_id_ctx.get()
    if chat_id is None:
        return "Fehler: Kein Nutzerkontext verfügbar."
    ad = ad_store.find_ad(chat_id, ad_key)
    if not ad:
        return f"Fehler: Kein Inserat mit Schlüssel {ad_key} gefunden."
    try:
        ad_store.set_active(ad.path, enabled)
    except OSError as e:
        return f"Fehler beim Schreiben: {e}"
    log.info("[chat=%d] Auto-republish %s for '%s'", chat_id, enabled, ad.title)
    state = "wird wieder" if enabled else "wird nicht mehr"
    return f"Inserat «{ad.title}» {state} automatisch neu geschaltet."

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
import os
import re
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from strands import tool

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


def _base_dir() -> Path:
    return Path(
        os.environ.get(
            "KLEINANZEIGEN_BASE_DIR",
            "/docker-volumes/kleinanzeiger/kleinanzeigen-config",
        )
    )


def _list_ads_internal(chat_id: int) -> list[dict]:
    """Read all ad.yaml files for a user and return structured dicts."""
    work_dir = _base_dir() / str(chat_id) / "ads"
    if not work_dir.is_dir():
        return []
    ads = []
    for idx, ad_yaml in enumerate(sorted(work_dir.glob("*/*.yaml"))):
        try:
            data = yaml.safe_load(ad_yaml.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        ads.append(
            {
                "index": idx,
                "path": ad_yaml,
                "title": data.get("title", "(kein Titel)"),
                "price": data.get("price", "?"),
                "active": bool(data.get("active", True)),
                "ad_id": data.get("id"),
            }
        )
    return ads


def _set_active_flag(ad_file: Path, *, active: bool) -> None:
    text = ad_file.read_text(encoding="utf-8")
    value = "true" if active else "false"
    text = re.sub(r"(?m)^active:\s*(true|false)\s*$", f"active: {value}", text)
    ad_file.write_text(text, encoding="utf-8")

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
# Kleinanzeigen publish tool
# ---------------------------------------------------------------------------

_queue_enqueue_fn = None


def set_queue_enqueue_fn(fn) -> None:
    """Register the queue enqueue function from main.py."""
    global _queue_enqueue_fn
    _queue_enqueue_fn = fn


@tool
def publish_kleinanzeigen_ad(ad_yaml_path: str) -> str:
    """Queue a Kleinanzeigen.de ad for publishing via the background worker.

    Args:
        ad_yaml_path: Absolute path to the ad.yaml file prepared for this listing.

    Returns a confirmation string with the job ID, or an error message.
    """
    chat_id = _chat_id_ctx.get()
    if chat_id is None:
        return "Fehler: Kein Nutzerkontext verfügbar."
    if _queue_enqueue_fn is None:
        return "Fehler: Warteschlange nicht initialisiert."

    job_id = str(uuid.uuid4())
    _queue_enqueue_fn(
        job_id=job_id,
        chat_id=chat_id,
        job_type="publish_ad",
        data={"ad_file": ad_yaml_path, "chat_id": chat_id},
    )
    log.info("[chat=%d job=%s] Publish job queued via tool", chat_id, job_id)
    return f"Anzeige wird geschaltet (Job-ID: {job_id[:8]})."


@tool
def list_kleinanzeigen_ads() -> list[dict]:
    """List all local Kleinanzeigen.de ads for the current user.

    Returns a list of ads. Each entry contains:
    - index: integer – use this to reference the ad in delete/deactivate tools
    - title: ad title
    - price: price in EUR
    - active: true if the ad is active (will be republished); false if deactivated
    - ad_id: Kleinanzeigen.de listing ID if already published (may be null)

    Call this before delete_kleinanzeigen_ad or deactivate_kleinanzeigen_ad
    to find the correct index for the ad the user is referring to.
    """
    chat_id = _chat_id_ctx.get()
    if chat_id is None:
        return []
    return [
        {k: v for k, v in ad.items() if k != "path"}
        for ad in _list_ads_internal(chat_id)
    ]


@tool
def delete_kleinanzeigen_ad(ad_index: int) -> str:
    """Delete a Kleinanzeigen.de ad from the site AND deactivate it locally.

    This queues a background job that opens a browser, removes the ad from
    Kleinanzeigen.de, and then sets active: false in the local YAML so it
    won't be republished.

    Use list_kleinanzeigen_ads() first to find the correct index.

    Args:
        ad_index: the 'index' value returned by list_kleinanzeigen_ads()

    Returns a confirmation or error message.
    """
    chat_id = _chat_id_ctx.get()
    if chat_id is None:
        return "Fehler: Kein Nutzerkontext verfügbar."
    if _queue_enqueue_fn is None:
        return "Fehler: Warteschlange nicht initialisiert."
    if not os.environ.get("KLEINANZEIGEN_BOT_CMD", ""):
        return "Fehler: KLEINANZEIGEN_BOT_CMD nicht konfiguriert."

    ads = _list_ads_internal(chat_id)
    matches = [a for a in ads if a["index"] == ad_index]
    if not matches:
        return f"Fehler: Kein Inserat mit Index {ad_index} gefunden. Bitte list_kleinanzeigen_ads() aufrufen."
    ad = matches[0]

    job_id = str(uuid.uuid4())
    _queue_enqueue_fn(
        job_id=job_id,
        chat_id=chat_id,
        job_type="delete_ad",
        data={"ad_file": str(ad["path"]), "chat_id": chat_id},
    )
    log.info("[chat=%d job=%s] Delete job queued for '%s'", chat_id, job_id, ad["title"])
    return f"Inserat «{ad['title']}» wird gelöscht (Job-ID: {job_id[:8]})."


@tool
def deactivate_kleinanzeigen_ad(ad_index: int) -> str:
    """Deactivate a Kleinanzeigen.de ad locally WITHOUT deleting it from the site.

    Sets active: false in the local YAML so the ad won't be republished.
    The listing remains live on Kleinanzeigen.de until it expires or is manually deleted.

    Use list_kleinanzeigen_ads() first to find the correct index.

    Args:
        ad_index: the 'index' value returned by list_kleinanzeigen_ads()

    Returns a confirmation or error message.
    """
    chat_id = _chat_id_ctx.get()
    if chat_id is None:
        return "Fehler: Kein Nutzerkontext verfügbar."

    ads = _list_ads_internal(chat_id)
    matches = [a for a in ads if a["index"] == ad_index]
    if not matches:
        return f"Fehler: Kein Inserat mit Index {ad_index} gefunden. Bitte list_kleinanzeigen_ads() aufrufen."
    ad = matches[0]

    try:
        _set_active_flag(ad["path"], active=False)
    except OSError as e:
        return f"Fehler beim Schreiben: {e}"

    log.info("[chat=%d] Deactivated ad '%s' locally", chat_id, ad["title"])
    return f"Inserat «{ad['title']}» lokal deaktiviert – wird nicht mehr republiziert."

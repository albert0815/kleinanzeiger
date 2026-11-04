"""Telegram bot: Kleinanzeigen ad drafting + lunch planning, via Strands agent."""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import random
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv

load_dotenv()

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.helpers import escape_markdown

import ad_store
import bot_runner
import category_fields
import lunch_plan
import mahlino
import recipes
from agent_registry import clear_agent, get_agent
from background_worker import BackgroundWorker
from queue_manager import QueueManager
from bot_runner import BotFailure
from tools import set_agent_chat_id, set_confirmation_fn, set_queue_enqueue_fn

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("kleinanzeigen-agent")
logging.getLogger("httpx").setLevel(logging.WARNING)

ALBUM_DEBOUNCE_SECONDS = 1.5
STREAM_EDIT_INTERVAL_SECONDS = 0.8
STREAM_EDIT_MIN_CHARS = 80
TELEGRAM_MESSAGE_LIMIT = 4096

KLEINANZEIGEN_BOT_CMD = os.environ.get("KLEINANZEIGEN_BOT_CMD", "")
SYNC_MAX_AGE = datetime.timedelta(minutes=15)
# Scheduled online checks (Europe/Berlin), each delayed by a random amount.
SYNC_TIMES = (datetime.time(7, 30), datetime.time(18, 30))
SYNC_MAX_JITTER_SECONDS = 45 * 60
CONDITION_VALUES = {
    "neu": "new",
    "sehr gut": "like_new",
    "gut": "ok",
    "in ordnung": "alright",
    "defekt": "defect",
}
FAILURE_HINTS = {
    "login": "Der Login bei Kleinanzeigen hat nicht geklappt. Eventuell ist eine "
    "manuelle Anmeldung mit SMS-Code oder Captcha nötig.",
    "browser": "Der Browser ließ sich nicht starten.",
    "timeout": "Kleinanzeigen hat nicht rechtzeitig geantwortet.",
}
WEEKDAYS = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")
ONLINE_STATE_LABELS = {"active": "🟢 online", "reserved": "🟡 reserviert"}
OTHER_STATE_LABEL = "⚪ deaktiviert"


def _user_work_dir(chat_id: int) -> Path:
    """Return the ads working directory for a given Telegram chat user."""
    return ad_store.base_dir() / str(chat_id) / "ads"


# Republishing replaces the online ad and costs watchers, so it is opt-in.
KLEINANZEIGEN_AUTO_REPUBLISH = (
    os.environ.get("KLEINANZEIGEN_AUTO_REPUBLISH", "false").lower() == "true"
)

KLEINANZEIGEN_REPUBLISH_DAYS = int(os.environ.get("KLEINANZEIGEN_REPUBLISH_DAYS", "14"))

KLEINANZEIGEN_REPUBLISH_TIME = datetime.time(
    int(os.environ.get("KLEINANZEIGEN_REPUBLISH_HOUR", "9")),
    int(os.environ.get("KLEINANZEIGEN_REPUBLISH_MINUTE", "0")),
    tzinfo=ZoneInfo("Europe/Berlin"),
)

QUEUE_DIR = Path(
    os.environ.get(
        "KLEINANZEIGEN_QUEUE_DIR",
        str(ad_store.base_dir() / ".queue"),
    )
)
LUNCH_PLAN_TZ = ZoneInfo("Europe/Berlin")

# Access control – comma-separated Telegram user IDs, e.g. "123456789,987654321"
_allowed_raw = os.environ.get("ALLOWED_USERS", "")
ALLOWED_USER_IDS: set[int] = {
    int(uid.strip()) for uid in _allowed_raw.split(",") if uid.strip().isdigit()
}

# Queue and worker (initialized in main())
QUEUE_MANAGER: QueueManager | None = None
BACKGROUND_WORKER: BackgroundWorker | None = None


# ---------------------------------------------------------------------------
# Draft state — photos + last parsed ad JSON from the agent.
# Conversation history lives in the Strands agent; this is only a view/cache
# used for rendering and writing ad files.
# ---------------------------------------------------------------------------


@dataclass
class Draft:
    photos: list[bytes] = field(default_factory=list)
    title: str = ""
    category: str = ""
    condition: str = ""
    description: str = ""
    price_eur: int = 0
    price_reasoning: str = ""
    missing_info: list[str] = field(default_factory=list)
    # Category-specific form fields from the bot's `fields` command.
    raw_attributes: dict = field(default_factory=dict)
    attributes: dict[str, str] = field(default_factory=dict)  # special_attributes key -> stored value
    missing_fields: list[dict] = field(default_factory=list)
    fields: list[dict] | None = None
    fields_category: str = ""
    fields_state: str = "none"  # none | pending | ready | failed


# Mutable per-chat state
DRAFTS: dict[int, Draft] = {}
PENDING_ALBUMS: dict[tuple[int, str], dict] = {}
CHAT_PHOTO_POOL: dict[int, list[bytes]] = {}
ACTIVE_GENERATION_TASKS: dict[int, asyncio.Task] = {}
AGENT_LOCKS: dict[int, asyncio.Lock] = {}

BOT = None  # set in main(); job handlers have no callback context

# Shown in Telegram's command menu and in /start.
BOT_COMMANDS = [
    BotCommand("inserate", "Inserate mit Online-Status anzeigen und verwalten"),
    BotCommand("neu", "Entwurf verwerfen und Konversation zurücksetzen"),
    BotCommand("lunch", "Heutiges Mittagessen anzeigen"),
    BotCommand("woche", "Essensplan der nächsten 7 Tage anzeigen"),
    BotCommand("plan", "Essensplan für nächste Woche vorschlagen"),
    BotCommand("queue", "Status der Warteschlange anzeigen"),
    BotCommand("backout", "Fehlgeschlagene Aufträge anzeigen und wiederholen"),
    BotCommand("help", "Hilfe und alle Befehle anzeigen"),
]



# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _apply_dict_to_draft(d: Draft, data: dict) -> None:
    d.title = str(data.get("title", d.title))[:65]
    d.category = str(data.get("category", d.category))
    d.condition = str(data.get("condition", d.condition))
    d.description = str(data.get("description", d.description))
    d.price_eur = int(data.get("price_eur", d.price_eur) or 0)
    d.price_reasoning = str(data.get("price_reasoning", d.price_reasoning))
    d.missing_info = list(data.get("missing_info", d.missing_info) or [])
    if isinstance(data.get("attributes"), dict):
        d.raw_attributes = data["attributes"]
    _validate_draft_fields(d)


def _validate_draft_fields(d: Draft) -> None:
    if d.fields is not None:
        d.attributes, d.missing_fields = category_fields.validate(d.fields, d.raw_attributes)


def _field_question(label: str, hint: str) -> str:
    return f"{label}" + (f" (z. B. {hint})" if hint else "")


def escape_md(s: str) -> str:
    return s.replace("*", "·").replace("_", " ").replace("`", "'")


def render_draft(d: Draft) -> str:
    missing = ""
    if d.missing_info:
        bullets = "\n".join(f"• {escape_md(m)}" for m in d.missing_info)
        missing = f"\n\n_Noch unklar:_\n{bullets}"
    details = ""
    if d.attributes:
        lines = []
        for attribute, value in d.attributes.items():
            f = category_fields.find_field(d.fields or [], attribute)
            label, shown = (f["label"], category_fields.option_label(f, value)) if f else (attribute, value)
            lines.append(f"• {escape_md(label)}: {escape_md(shown)}")
        details = "\n\n*Details:*\n" + "\n".join(lines)
    if d.fields_state == "pending":
        details += "\n\n_Pflichtfelder der Kategorie werden ermittelt …_"
    if d.missing_fields:
        lines = "\n".join(
            f"• {escape_md(_field_question(f['label'], category_fields.options_hint(f)))}"
            for f in d.missing_fields
        )
        details += f"\n\n*Pflichtfeld fehlt – bitte angeben:*\n{lines}"
    return (
        f"*{escape_md(d.title)}*\n"
        f"_{escape_md(d.category)} · {escape_md(d.condition)}_\n\n"
        f"{escape_md(d.description)}\n\n"
        f"*Preis:* {d.price_eur} EUR\n"
        f"_{escape_md(d.price_reasoning)}_"
        f"{details}"
        f"{missing}"
    )


def render_final(d: Draft) -> str:
    return (
        "📋 *Zum Kopieren:*\n\n"
        "```\n"
        f"{d.title}\n\n"
        f"{d.description}\n\n"
        f"Preis: {d.price_eur} EUR\n"
        f"Kategorie-Vorschlag: {d.category}\n"
        f"Zustand: {d.condition}\n"
        "```"
    )


def ad_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("✅ Copy-Paste", callback_data="ok"),
            InlineKeyboardButton("🔁 Neu generieren", callback_data="regen"),
        ],
        [InlineKeyboardButton("❌ Verwerfen", callback_data="cancel")],
    ]
    if KLEINANZEIGEN_BOT_CMD:
        rows.insert(
            0, [InlineKeyboardButton("🚀 Direkt schalten", callback_data="publish")]
        )
    return InlineKeyboardMarkup(rows)


def plan_keyboard(proposal_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Annehmen & speichern", callback_data=f"plan_accept:{proposal_id}"
                ),
                InlineKeyboardButton("✏️ Ändern", callback_data="plan_change"),
            ],
            [InlineKeyboardButton("❌ Verwerfen", callback_data=f"plan_reject:{proposal_id}")],
        ]
    )


def _parse_json_from_response(response: str) -> dict | None:
    """Extract the first JSON object from an agent response, or return None."""
    text = re.sub(
        r"^```(?:json)?\s*|\s*```$", "", response.strip(), flags=re.MULTILINE
    ).strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except (json.JSONDecodeError, ValueError):
        return None


def _yaml_escape(s: str) -> str:
    """Quote a string safely for YAML using JSON (which is valid YAML)."""
    return json.dumps(s, ensure_ascii=False)


def write_ad_files(d: Draft, chat_id: int) -> Path:
    """Persist photos + ad.yaml into a fresh working directory. Returns ad.yaml path."""
    category = ad_store.resolve_category(d.category)
    if not category:
        raise ValueError(
            f"Die Kategorie «{d.category}» kenne ich nicht. Nenne mir bitte eine passende."
        )
    work_dir = _user_work_dir(chat_id)
    work_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    safe_title = re.sub(r"[^a-zA-Z0-9_-]+", "_", d.title)[:40] or "ad"
    ad_dir = work_dir / f"{ts}_{safe_title}"
    ad_dir.mkdir(parents=True)

    for i, img in enumerate(d.photos, start=1):
        (ad_dir / f"{i:02d}.jpg").write_bytes(img)

    title = d.title.replace("\n", " ").strip()
    desc = d.description.strip()
    condition = CONDITION_VALUES.get(d.condition.strip().lower())
    lines = [f"  condition_s: {condition}"] if condition else []
    lines += [f"  {name}: {_yaml_escape(value)}" for name, value in d.attributes.items()]
    attributes = "special_attributes:\n" + "\n".join(lines) + "\n" if lines else ""
    yaml_text = (
        "active: true\n"
        f"title: {_yaml_escape(title)}\n"
        f"description: {_yaml_escape(desc)}\n"
        f"category: {_yaml_escape(category)}\n"
        f"price: {int(d.price_eur)}\n"
        f"{attributes}"
        'images:\n  - "*.jpg"\n'
    )
    ad_file = ad_dir / "ad_new.yaml"
    ad_file.write_text(yaml_text, encoding="utf-8")
    return ad_file


def _describe_failure(error: BotFailure) -> str:
    hint = FAILURE_HINTS.get(error.kind)
    return f"{hint} ({error})" if hint else str(error)


def _strip_condition(ad_file: Path) -> None:
    """Remove the condition attribute, which some categories do not offer."""
    text = ad_file.read_text(encoding="utf-8")
    ad_file.write_text(
        re.sub(r"(?m)^special_attributes:\n\s+condition_s:.*\n", "", text),
        encoding="utf-8",
    )


async def _send_sync_report(chat_id: int, report: dict, announce: bool) -> None:
    if report["first_sync"]:
        count = len(ad_store.online_view(chat_id)["ads"])
        await BOT.send_message(
            chat_id, f"🔄 Abgleich fertig: {count} Inserate sind online. Details unter /inserate."
        )
        return
    for ad in report["missing"]:
        await BOT.send_message(
            chat_id,
            f"ℹ️ Nicht mehr auf Kleinanzeigen: {ad['title']}",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "💰 Als verkauft eintragen", callback_data=f"sold:{ad['key']}"
                        )
                    ]
                ]
            ),
        )
    for ad in report["online_only"]:
        await BOT.send_message(chat_id, f"🆕 Neu auf Kleinanzeigen: {ad['title']}")
    for ad in report["expiring"]:
        await BOT.send_message(
            chat_id, f"⏳ Läuft am {ad['expires']} ab: {ad['title']}"
        )
    if announce and not any(report[k] for k in ("missing", "online_only", "expiring")):
        await BOT.send_message(chat_id, "✅ Online-Abgleich: keine neuen Abweichungen.")


async def _store_online_snapshot(
    chat_id: int, online: list[ad_store.OnlineAd], *, announce: bool = False
) -> None:
    if ad_store.update_health(chat_id, True, None, None):
        await BOT.send_message(chat_id, "✅ Der Zugriff auf Kleinanzeigen klappt wieder.")
    await _send_sync_report(chat_id, ad_store.apply_sync(chat_id, online), announce)


async def _import_online_only(chat_id: int, online: list[ad_store.OnlineAd]) -> None:
    """Take over ads created outside the agent, e.g. in the app."""
    local_ids = {ad.ad_id for ad in ad_store.list_local_ads(chat_id)}
    new_ids = [ad.ad_id for ad in online if ad.ad_id not in local_ids]
    if not new_ids:
        return
    try:
        await bot_runner.download_ids(chat_id, new_ids)
    except BotFailure as e:
        log.error("[chat=%d] importing online ads failed: %s", chat_id, e)
        return
    imported = []
    for ad_id in new_ids:
        downloaded = ad_store.find_download(chat_id, ad_id)
        try:
            ad = ad_store.import_download(chat_id, downloaded) if downloaded else None
        except OSError as e:
            log.error("[chat=%d] importing %s failed: %s", chat_id, ad_id, e)
            continue
        if ad:
            imported.append(ad.title)
    if imported:
        text = "📥 Von Kleinanzeigen übernommen:\n" + "\n".join(f"• {t}" for t in imported)
        await BOT.send_message(chat_id, text[:TELEGRAM_MESSAGE_LIMIT])


async def sync_account(chat_id: int, *, announce: bool = False) -> bool:
    """Refresh the online snapshot. Failures are reported once per failure type."""
    try:
        online = await bot_runner.list_online(chat_id)
    except BotFailure as e:
        log.error("[chat=%d] online check failed: %s", chat_id, e)
        if e.kind == "config":
            return False
        if ad_store.update_health(chat_id, False, e.kind, str(e)) or announce:
            await BOT.send_message(
                chat_id, f"⚠️ Online-Abgleich fehlgeschlagen: {_describe_failure(e)}"
            )
        return False
    await _import_online_only(chat_id, online)
    await _store_online_snapshot(chat_id, online, announce=announce)
    return True


# ---------------------------------------------------------------------------
# Agent bridge
# ---------------------------------------------------------------------------


def _build_agent_input(
    chat_id: int,
    message: str,
    photos: list[bytes] | None = None,
) -> str | list:
    if photos:
        content: list[dict] = []
        for img in photos[:8]:
            log.info("[chat=%d] photo %d KB → agent", chat_id, len(img) // 1024)
            content.append(
                {
                    "image": {
                        "format": "jpeg",
                        "source": {"bytes": img},
                    },
                }
            )
        content.append({"text": message})
        user_input: str | list = content
    else:
        user_input = message
    return user_input


async def _call_agent(
    chat_id: int,
    message: str,
    photos: list[bytes] | None = None,
) -> str:
    """Invoke the Strands agent for *chat_id* and return the text response.

    The agent is called via asyncio.to_thread so the synchronous Strands call
    does not block the Telegram event loop. All tool functions in tools.py are
    intentionally synchronous for this reason.
    """
    agent = get_agent(chat_id)
    user_input = _build_agent_input(chat_id, message, photos)
    # One turn at a time per chat: field completion may run alongside a message.
    async with AGENT_LOCKS.setdefault(chat_id, asyncio.Lock()):
        set_agent_chat_id(chat_id)
        result = await asyncio.to_thread(agent, user_input)
    return str(result)


def _fit_telegram_message(text: str) -> str:
    if len(text) <= TELEGRAM_MESSAGE_LIMIT:
        return text
    suffix = "\n\n…"
    return text[: TELEGRAM_MESSAGE_LIMIT - len(suffix)] + suffix


async def _edit_stream_message(
    message: Message,
    text: str,
    *,
    parse_mode: ParseMode | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    try:
        await message.edit_text(
            _fit_telegram_message(text),
            parse_mode=parse_mode,
            reply_markup=reply_markup,
        )
    except BadRequest as e:
        if "Message is not modified" in str(e):
            return
        if parse_mode is not None:
            await message.edit_text(
                _fit_telegram_message(text),
                reply_markup=reply_markup,
            )
            return
        raise


async def _call_agent_streaming(
    chat_id: int,
    message: str,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    final_parse_mode: ParseMode | None = ParseMode.MARKDOWN,
) -> str:
    """Stream a text-only Strands response into Telegram by editing one message."""
    agent = get_agent(chat_id)
    set_agent_chat_id(chat_id)
    stream_message = await context.bot.send_message(chat_id, "🤖 Moment, bitte…")
    stream_async = getattr(agent, "stream_async", None)
    if not callable(stream_async):
        response = await _call_agent(chat_id, message)
        await _edit_stream_message(
            stream_message,
            response or "✓",
            parse_mode=final_parse_mode,
            reply_markup=reply_markup,
        )
        return response

    chunks: list[str] = []
    final_text = ""
    current_len = 0
    last_edit_at = 0.0
    last_edit_len = 0

    async with AGENT_LOCKS.setdefault(chat_id, asyncio.Lock()):
        set_agent_chat_id(chat_id)
        try:
            async for event in stream_async(_build_agent_input(chat_id, message)):
                if "data" in event:
                    chunk = str(event["data"])
                    chunks.append(chunk)
                    current_len += len(chunk)
                    current_text = "".join(chunks)
                    if not current_text:
                        continue
                    now = time.monotonic()
                    if (
                        now - last_edit_at >= STREAM_EDIT_INTERVAL_SECONDS
                        or current_len - last_edit_len >= STREAM_EDIT_MIN_CHARS
                    ):
                        await _edit_stream_message(stream_message, current_text)
                        last_edit_at = now
                        last_edit_len = current_len
                elif "result" in event:
                    final_text = str(event["result"]).strip()
        except Exception:
            await _edit_stream_message(stream_message, "⚠️ Fehler beim Generieren.")
            raise


    response = final_text or "".join(chunks).strip()
    await _edit_stream_message(
        stream_message,
        response or "✓",
        parse_mode=final_parse_mode,
        reply_markup=reply_markup,
    )
    return response


# ---------------------------------------------------------------------------
# Lunch plan helpers
# ---------------------------------------------------------------------------


def _has_meal(plan_entries: list[dict], date: datetime.date) -> bool:
    date_str = date.isoformat()
    entry = next((e for e in plan_entries if e.get("date") == date_str), None)
    return bool(entry and entry.get("recipes"))


def format_lunch_message(plan_entries: list[dict], date: datetime.date) -> str:
    """Format the meal plan as a Telegram-ready Markdown message."""
    date_label = date.strftime("%d.%m.%Y")
    today_str = date.isoformat()
    entry = next((e for e in plan_entries if e.get("date") == today_str), None)

    if not entry or not entry.get("recipes"):
        return f"🍽️ *Mittagessen am {date_label}*\n\nKein Mittagessen geplant."

    lines = [f"🍽️ *Mittagessen am {date_label}*\n"]
    for recipe in entry["recipes"]:
        name = escape_md(recipe.get("name", "?"))
        duration = recipe.get("duration", "")
        lines.append(f"• *{name}*" + (f" · {duration} min" if duration else ""))
    return "\n".join(lines)


def _weekday_label(day: str) -> str:
    date = datetime.date.fromisoformat(day)
    return f"{WEEKDAYS[date.weekday()]} {date:%d.%m.}"


def format_week(plan_entries: list[dict]) -> str:
    lines = ["🍽️ Essensplan"]
    for entry in plan_entries:
        names = ", ".join(r["name"] for r in entry["recipes"])
        if not names and lunch_plan.is_no_cooking_day(
            datetime.date.fromisoformat(entry["date"])
        ):
            names = "Mama kocht"
        lines.append(f"{_weekday_label(entry['date'])}: {names or '–'}")
    return "\n".join(lines)


def format_proposal(proposal: dict) -> str:
    lines = ["🍽️ Vorschlag für den Essensplan:"]
    for item in proposal["days"]:
        target = item["name"] or "Eintrag entfernen"
        previous = ", ".join(item["previous"])
        change = f" (statt {previous})" if previous else ""
        lines.append(f"{_weekday_label(item['date'])}: {target}{change}")
    return "\n".join(lines)


async def announce_lunch_proposal() -> None:
    """Show a new proposal to the household; anyone can confirm it."""
    proposal = lunch_plan.take_unannounced()
    if not proposal:
        return
    for user_id in ALLOWED_USER_IDS:
        try:
            await BOT.send_message(
                user_id, format_proposal(proposal), reply_markup=plan_keyboard(proposal["id"])
            )
        except Exception as e:
            log.error("Failed to send lunch proposal to user %d: %s", user_id, e)


async def announce_recipe_proposal(chat_id: int) -> None:
    proposal = recipes.take_unannounced(chat_id)
    if not proposal:
        return
    await BOT.send_message(
        chat_id,
        recipes.format_proposal(proposal["recipe"]),
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Rezept speichern", callback_data=f"recipe_save:{proposal['id']}"
                    ),
                    InlineKeyboardButton(
                        "❌ Verwerfen", callback_data=f"recipe_drop:{proposal['id']}"
                    ),
                ]
            ]
        ),
    )


async def _trigger_week_plan(
    user_id: int,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Ask the agent to propose a meal plan for the coming week and present it."""
    trigger = "Bitte erstelle einen Vorschlag für das Mittagessen."
    try:
        await _call_agent_streaming(user_id, trigger, context)
        await announce_lunch_proposal()
        log.info("[chat=%d] Lunch plan suggestion finished.", user_id)
    except Exception as e:
        log.error("[chat=%d] Failed to send lunch plan suggestion: %s", user_id, e)
        await BOT.send_message(user_id, "⚠️ Der Essensplan-Vorschlag ist fehlgeschlagen.")


async def send_lunch_plan(context: ContextTypes.DEFAULT_TYPE) -> None:
    """JobQueue callback: send today's lunch plan at 08:30.

    If tomorrow needs a meal, trigger one proposal for the whole household.
    """
    today = datetime.datetime.now(LUNCH_PLAN_TZ).date()
    tomorrow = today + datetime.timedelta(days=1)
    log.info("Sending lunch plan for %s to %d user(s)", today, len(ALLOWED_USER_IDS))

    try:
        plan_range = await mahlino.fetch_plan(today, tomorrow)
    except Exception as e:
        log.error("Failed to fetch lunch plan: %s", e)
        for user_id in ALLOWED_USER_IDS:
            await context.bot.send_message(
                user_id, "⚠️ Der Essensplan konnte heute nicht abgerufen werden."
            )
        return

    today_msg = format_lunch_message(plan_range, today)
    for user_id in ALLOWED_USER_IDS:
        try:
            await context.bot.send_message(
                user_id, today_msg, parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            log.error("Failed to send today's lunch to user %d: %s", user_id, e)

    if lunch_plan.should_propose(plan_range, tomorrow):
        await _trigger_week_plan(min(ALLOWED_USER_IDS), context)


async def republish_kleinanzeigen(context: ContextTypes.DEFAULT_TYPE) -> None:
    """JobQueue callback: queue the daily republish run for every account."""
    for user_id in ALLOWED_USER_IDS:
        # No automatic retry: a repeated run could put an ad online twice.
        QUEUE_MANAGER.enqueue(
            job_id=str(uuid.uuid4()),
            chat_id=user_id,
            job_type="republish_ads",
            data={"chat_id": user_id},
            max_retries=1,
        )


async def scheduled_sync(context: ContextTypes.DEFAULT_TYPE) -> None:
    """JobQueue callback: check all accounts at a slightly varying time."""
    await asyncio.sleep(random.uniform(0, SYNC_MAX_JITTER_SECONDS))
    for user_id in ALLOWED_USER_IDS:
        try:
            await sync_account(user_id)
        except Exception:
            log.exception("[chat=%d] scheduled online check failed", user_id)


async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    commands = "\n".join(f"/{c.command} — {c.description}" for c in BOT_COMMANDS)
    await update.message.reply_text(
        "Hallo! Das kann ich:\n\n"
        "📸 Kleinanzeigen-Inserate\n"
        "Schick mir ein Foto oder ein Album, dann erstelle ich einen Entwurf. "
        "Ich gleiche deine Inserate außerdem mit Kleinanzeigen ab, auch solche "
        "aus der App, und schalte sie regelmäßig neu. Im Chat kannst du z. B. "
        "schreiben: „Das Fahrrad ist verkauft“.\n\n"
        "🍽️ Mittagessen-Planung\n"
        "Jeden Morgen um 08:30 schicke ich dir den aktuellen Essensplan. Falls für "
        "die nächste Woche noch etwas fehlt, schlage ich einen Plan vor.\n\n"
        "📖 Rezepte\n"
        "Schick mir einen Link zu einem Rezept oder beschreib es, dann lege ich es "
        "nach deiner Bestätigung in Mahlino an.\n\n"
        f"Befehle:\n{commands}"
    )


def _sync_is_stale(chat_id: int) -> bool:
    synced_at = ad_store.load_state(chat_id)["synced_at"]
    if not synced_at:
        return True
    age = datetime.datetime.now(datetime.UTC) - datetime.datetime.fromisoformat(synced_at)
    return age > SYNC_MAX_AGE


def _state_label(state: str) -> str:
    return ONLINE_STATE_LABELS.get(state, OTHER_STATE_LABEL)


def _render_ad_list(chat_id: int) -> tuple[str, InlineKeyboardMarkup | None]:
    """List only what is on Kleinanzeigen; local files are an implementation detail."""
    view = ad_store.online_view(chat_id)
    if not view["synced_at"]:
        return "Ich konnte Kleinanzeigen noch nicht abrufen. Bitte versuch es später erneut.", None
    checked = (
        datetime.datetime.fromisoformat(view["synced_at"])
        .astimezone(LUNCH_PLAN_TZ)
        .strftime("%d.%m. %H:%M")
    )
    lines = [f"Deine Inserate auf Kleinanzeigen (Stand {checked}): {len(view['ads'])}"]
    if not view["health"]["ok"]:
        lines.insert(0, "⚠️ Der letzte Abruf ist fehlgeschlagen, der Stand kann veraltet sein.")
    # Telegram allows at most 100 inline buttons per message.
    managed = sorted((a for a in view["ads"] if a["key"]), key=lambda a: a["title"])
    rows = [
        [
            InlineKeyboardButton(
                f"{_state_label(a['state']).split()[0]} {a['title'][:45]}",
                callback_data=f"ad:{a['key']}",
            )
        ]
        for a in managed[:100]
    ]
    unmanaged = [a for a in view["ads"] if not a["key"]]
    if unmanaged:
        lines.append("\nNoch nicht bearbeitbar, kommt beim nächsten Abgleich:")
        lines.extend(f"• {a['title']}" for a in unmanaged)
    return "\n".join(lines), InlineKeyboardMarkup(rows) if rows else None


async def _show_ad_list(chat_id: int) -> None:
    try:
        if _sync_is_stale(chat_id):
            await sync_account(chat_id)
        text, markup = _render_ad_list(chat_id)
        await BOT.send_message(chat_id, text[:TELEGRAM_MESSAGE_LIMIT], reply_markup=markup)
    except Exception:
        log.exception("[chat=%d] showing ads failed", chat_id)
        await BOT.send_message(chat_id, "Fehler beim Anzeigen der Inserate.")


async def cmd_inserate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the ads on Kleinanzeigen; tapping one opens its actions."""
    chat_id = update.effective_chat.id
    if not KLEINANZEIGEN_BOT_CMD:
        await update.message.reply_text("Der Zugriff auf Kleinanzeigen ist nicht eingerichtet.")
        return
    if _sync_is_stale(chat_id):
        await update.message.reply_text("🔍 Prüfe den Stand auf Kleinanzeigen…")
    # A browser run takes a while, so don't block other incoming updates.
    context.application.create_task(_show_ad_list(chat_id))


def _ad_actions(entry: dict) -> InlineKeyboardMarkup:
    key = entry["key"]
    rows = [
        [
            InlineKeyboardButton("💰 Verkauft", callback_data=f"sold:{key}"),
            InlineKeyboardButton("🗑 Löschen", callback_data=f"del:{key}"),
        ]
    ]
    if entry["state"] == "active":
        rows.append(
            [
                InlineKeyboardButton("⏸ Nicht mehr neu schalten", callback_data=f"deact:{key}")
                if entry["auto_republish"]
                else InlineKeyboardButton("▶️ Wieder neu schalten", callback_data=f"react:{key}")
            ]
        )
    return InlineKeyboardMarkup(rows)


async def cmd_neu(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    DRAFTS.pop(chat_id, None)
    CHAT_PHOTO_POOL.pop(chat_id, None)
    task = ACTIVE_GENERATION_TASKS.pop(chat_id, None)
    if task and not task.done():
        task.cancel()
    clear_agent(chat_id)
    log.info("[chat=%d] /neu — state cleared", chat_id)
    await update.message.reply_text(
        "Ok, Konversation zurückgesetzt. Schick mir ein Foto oder stell eine Frage."
    )


async def cmd_lunch(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """Show today's lunch plan on demand via /lunch."""
    today = datetime.datetime.now(LUNCH_PLAN_TZ).date()
    await update.message.reply_text("🔍 Hole Mittagessen…")
    try:
        plan = await mahlino.fetch_plan(today, today)
    except Exception as e:
        log.error("cmd_lunch: fetch failed: %s", e)
        await update.message.reply_text(f"Fehler beim Abrufen: {e}")
        return
    msg = format_lunch_message(plan, today)
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


async def cmd_woche(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the lunches of the next seven days."""
    today = datetime.datetime.now(LUNCH_PLAN_TZ).date()
    try:
        plan = await mahlino.fetch_plan(today, today + datetime.timedelta(days=6))
    except Exception as e:
        log.error("cmd_woche: fetch failed: %s", e)
        await update.message.reply_text("Der Essensplan konnte nicht abgerufen werden.")
        return
    await update.message.reply_text(format_week(plan))


async def cmd_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manually trigger a lunch plan suggestion for next week via /plan."""
    chat_id = update.effective_chat.id
    await update.message.reply_text("🤔 Erstelle Essensplan-Vorschlag…")
    await _trigger_week_plan(chat_id, context)


async def cmd_queue_status(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """Show current queue status."""
    pending_count = QUEUE_MANAGER.get_pending_count()
    backout_count = QUEUE_MANAGER.get_backout_count()

    await update.message.reply_text(
        f"📊 Queue-Status:\n"
        f"⏳ Ausstehend: {pending_count}\n"
        f"❌ Warteschlange (fehlgeschlagen): {backout_count}\n\n"
        f"Nutze /backout um fehlgeschlagene Jobs zu sehen."
    )


async def cmd_backout_jobs(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """Show backout jobs for this chat."""
    chat_id = update.effective_chat.id
    backout_jobs = QUEUE_MANAGER.get_backout_jobs(chat_id=chat_id)

    if not backout_jobs:
        await update.message.reply_text("Keine fehlgeschlagenen Jobs für diesen Chat.")
        return

    msg_lines = ["❌ Fehlgeschlagene Jobs:\n"]
    for job in backout_jobs:
        retry_cmd = f"/retry_{job.job_id[:8]}"
        safe_error = escape_markdown(str(job.error), version=1)
        msg_lines.append(
            f"• Job `{job.job_id[:8]}` ({job.job_type})\n"
            f"  Fehler: {safe_error}\n"
            f"  Versuche: {job.retry_count}/{job.max_retries}\n"
            f"  Befehl: `{retry_cmd}`\n"
        )

    await update.message.reply_text(
        "".join(msg_lines),
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_retry_job(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """Retry a specific backout job. Usage: /retry_<job_id_prefix>"""
    cmd_text = (update.message.text or "").strip()
    match = re.match(r"/retry_(\S+)", cmd_text)
    if not match:
        await update.message.reply_text("Befehl: /retry_<job_id>")
        return

    job_id_prefix = match.group(1)
    chat_id = update.effective_chat.id
    backout_jobs = QUEUE_MANAGER.get_backout_jobs(chat_id=chat_id)

    # Find matching job
    matching = [j for j in backout_jobs if j.job_id.startswith(job_id_prefix)]
    if not matching:
        await update.message.reply_text(f"Kein Job mit ID `{job_id_prefix}` gefunden.")
        return

    job = matching[0]
    if QUEUE_MANAGER.retry_backout_job(job.job_id):
        await update.message.reply_text(
            f"✅ Job `{job.job_id[:8]}` zur Wiederholung eingeplant."
        )
    else:
        await update.message.reply_text("Konnte Job nicht zur Wiederholung hinzufügen.")


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route free-form text through the Strands agent."""
    chat_id = update.effective_chat.id
    user_request = (update.message.text or "").strip()
    log.info("[chat=%d] text message: %r", chat_id, user_request[:200])
    if not user_request:
        return

    d = DRAFTS.get(chat_id)
    await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
    if not d:
        try:
            if await answer_publish_question(chat_id, user_request):
                return
        except Exception:
            log.exception("[chat=%d] handling the answer failed", chat_id)
    try:
        if d:
            response = await _call_agent(chat_id, user_request)
        else:
            response = await _call_agent_streaming(chat_id, user_request, context)
    except Exception as e:
        log.exception("[chat=%d] agent call failed", chat_id)
        await context.bot.send_message(chat_id, f"Fehler: {e}")
        return
    await announce_lunch_proposal()
    await announce_recipe_proposal(chat_id)

    # If there is an active draft and the response is JSON, update it
    if d:
        parsed = _parse_json_from_response(response)
        if parsed and "title" in parsed:
            category_before = d.fields_category
            _apply_dict_to_draft(d, parsed)
            if d.fields is not None and d.category != category_before:
                # A different category has different fields.
                d.fields, d.raw_attributes, d.attributes, d.missing_fields = None, {}, {}, []
                start_field_completion(chat_id, d)
            await context.bot.send_message(
                chat_id,
                render_draft(d),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=ad_keyboard(),
            )
            return

        await context.bot.send_message(chat_id, response, parse_mode=ParseMode.MARKDOWN)


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    chat_id = update.effective_chat.id

    photo = msg.photo[-1]
    log.info(
        "[chat=%d] photo received (file_id=%s, %dx%d, album=%s)",
        chat_id,
        photo.file_id,
        photo.width,
        photo.height,
        msg.media_group_id or "none",
    )
    file = await context.bot.get_file(photo.file_id)
    buf = BytesIO()
    await file.download_to_memory(buf)
    img_bytes = buf.getvalue()

    if msg.media_group_id:
        key = (chat_id, msg.media_group_id)
        entry = PENDING_ALBUMS.get(key)
        if entry is None:
            entry = {"photos": [], "task": None}
            PENDING_ALBUMS[key] = entry
        entry["photos"].append(img_bytes)
        if entry["task"]:
            entry["task"].cancel()
        entry["task"] = asyncio.create_task(_process_album_after_delay(key, context))
        return

    # Cancel any in-progress generation for this chat
    existing = ACTIVE_GENERATION_TASKS.get(chat_id)
    if existing and not existing.done():
        existing.cancel()

    # Accumulate photo: seed pool from existing draft if this is a fresh pool
    is_addition = chat_id in CHAT_PHOTO_POOL or chat_id in DRAFTS
    if chat_id not in CHAT_PHOTO_POOL:
        d = DRAFTS.get(chat_id)
        CHAT_PHOTO_POOL[chat_id] = list(d.photos) if d else []
    CHAT_PHOTO_POOL[chat_id].append(img_bytes)

    # Immediate feedback — differentiate first photo from additions
    total = len(CHAT_PHOTO_POOL[chat_id])
    if is_addition:
        await context.bot.send_message(
            chat_id,
            f"📸 Noch ein Foto – generiere das Inserat neu mit {total} Bildern…",
        )
    else:
        await context.bot.send_message(
            chat_id,
            "📸 Foto erhalten, einen Moment…",
        )

    # Schedule new generation with all accumulated photos
    photos_snapshot = list(CHAT_PHOTO_POOL[chat_id])
    task = asyncio.create_task(_process_photos(chat_id, photos_snapshot, context))
    ACTIVE_GENERATION_TASKS[chat_id] = task

    def _on_task_done(t: asyncio.Task) -> None:
        if ACTIVE_GENERATION_TASKS.get(chat_id) is t:
            ACTIVE_GENERATION_TASKS.pop(chat_id, None)

    task.add_done_callback(_on_task_done)


async def _process_album_after_delay(
    key: tuple[int, str], context: ContextTypes.DEFAULT_TYPE
) -> None:
    try:
        await asyncio.sleep(ALBUM_DEBOUNCE_SECONDS)
    except asyncio.CancelledError:
        return
    entry = PENDING_ALBUMS.pop(key, None)
    if not entry:
        return
    await context.bot.send_message(key[0], "🤖 Analysiere Album…")
    await _process_photos(key[0], entry["photos"], context)


async def _process_photos(
    chat_id: int, photos: list[bytes], context: ContextTypes.DEFAULT_TYPE
) -> None:
    log.info("[chat=%d] processing %d photo(s) via agent", chat_id, len(photos))
    await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
    try:
        response = await _call_agent(
            chat_id, "Erstelle das Inserat-JSON.", photos=photos
        )
    except Exception as e:
        log.exception("[chat=%d] agent photo processing failed", chat_id)
        await context.bot.send_message(chat_id, f"Fehler bei der Analyse: {e}")
        return

    parsed = _parse_json_from_response(response)
    if not parsed or "title" not in parsed:
        # Agent returned plain text — unlikely but handle gracefully
        await context.bot.send_message(chat_id, response, parse_mode=ParseMode.MARKDOWN)
        return

    d = Draft(photos=photos)
    _apply_dict_to_draft(d, parsed)

    # Reorder photos as the agent suggested
    order = parsed.get("photo_order")
    if order and isinstance(order, list) and len(order) == len(photos):
        try:
            reordered = [photos[int(i)] for i in order if 0 <= int(i) < len(photos)]
            if len(reordered) == len(photos):
                d.photos = reordered
        except (TypeError, ValueError, IndexError):
            pass  # keep original order on any error
    if not d.title:
        d.title = "?"
    DRAFTS[chat_id] = d
    log.info(
        "[chat=%d] draft created: title=%r price=%s",
        chat_id,
        d.title,
        d.price_eur,
    )
    start_field_completion(chat_id, d)
    await context.bot.send_message(
        chat_id,
        render_draft(d),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ad_keyboard(),
    )


async def complete_draft_fields(chat_id: int, d: Draft) -> None:
    """Get the category's fields via the bot and let the agent fill them in."""
    category = ad_store.resolve_category(d.category)
    if not category or not KLEINANZEIGEN_BOT_CMD:
        d.fields_state = "none"
        return
    d.fields_state = "pending"
    fields = category_fields.cached(category)
    if fields is None:
        try:
            fields = await bot_runner.category_fields(chat_id, category)
        except BotFailure as e:
            log.error("[chat=%d] reading fields for %s failed: %s", chat_id, category, e)
            if DRAFTS.get(chat_id) is d:
                d.fields_state = "failed"
                await BOT.send_message(
                    chat_id,
                    f"⚠️ Die Felder der Kategorie konnten nicht gelesen werden: {e}\n"
                    "Du kannst trotzdem schalten. Fehlt etwas, frage ich dann nach.",
                )
            return
        category_fields.store(category, fields)
    if DRAFTS.get(chat_id) is not d:
        return
    d.fields, d.fields_category = fields, d.category
    todo = category_fields.describe_for_agent(fields)
    if todo:
        prompt = (
            f"Die Kategorie «{category}» hat diese Zusatzfelder: "
            f"{json.dumps(todo, ensure_ascii=False)}\n"
            "Gib das vollständige Inserat-JSON zurück und ergänze \"attributes\" als Objekt "
            "{Feldname: exakter Optionstext}. Pflichtfelder (required=true) füllst du, wenn Fotos "
            "oder Angaben es hergeben, sonst lässt du sie weg. Optionale Felder nur, wenn sie "
            "eindeutig erkennbar sind. Ändere sonst nichts."
        )
        try:
            parsed = _parse_json_from_response(await _call_agent(chat_id, prompt))
        except Exception:
            log.exception("[chat=%d] filling fields failed", chat_id)
            parsed = None
        if DRAFTS.get(chat_id) is not d:
            return
        if parsed and "title" in parsed:
            _apply_dict_to_draft(d, parsed)
    _validate_draft_fields(d)
    d.fields_state = "ready"
    if todo:
        await BOT.send_message(
            chat_id, render_draft(d), parse_mode=ParseMode.MARKDOWN, reply_markup=ad_keyboard()
        )


def start_field_completion(chat_id: int, d: Draft) -> asyncio.Task:
    if KLEINANZEIGEN_BOT_CMD and ad_store.resolve_category(d.category):
        d.fields_state = "pending"
    task = asyncio.create_task(complete_draft_fields(chat_id, d))
    task.add_done_callback(
        lambda t: t.cancelled() or not t.exception()
        or log.error("[chat=%d] field completion crashed: %r", chat_id, t.exception())
    )
    return task


# ---------------------------------------------------------------------------
# Questions after a rejected publish: ask the user, then publish again
# ---------------------------------------------------------------------------


def _questions_path() -> Path:
    return ad_store.base_dir() / ".publish-questions.json"


def _load_questions() -> dict:
    try:
        return json.loads(_questions_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_questions(data: dict) -> None:
    temporary = _questions_path().with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    temporary.replace(_questions_path())


def _attribute_for_key(key: str) -> str:
    return f"{key.rsplit('.', 1)[-1]}_s"


def _questions_from_problems(problems: list[dict], category: str | None) -> list[dict]:
    """One question per field: label, special_attributes key and known options."""
    fields = category_fields.cached(category) if category else None
    questions = []
    for problem in problems:
        if problem["kind"] == "unknown":
            continue
        attribute = _attribute_for_key(problem["key"])
        known = category_fields.find_field(fields or [], problem["key"])
        options = [o["label"] for o in known["options"]] if known else []
        if problem["kind"] == "invalid":
            options = problem["options"] or options
        label = problem.get("label") or (known["label"] if known else problem["key"])
        questions.append({"attribute": attribute, "label": label, "options": options,
                          "rejected": problem.get("value")})
    return questions


def _edit_special_attributes(ad_file: Path, set_values: dict[str, str], remove: list[str]) -> None:
    data = yaml.safe_load(ad_file.read_text(encoding="utf-8")) or {}
    attributes = dict(data.get("special_attributes") or {})
    for name in remove:
        attributes.pop(name, None)
    attributes.update(set_values)
    data["special_attributes"] = attributes or None
    ad_file.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _enqueue_publish(chat_id: int, ad_file: Path) -> str:
    job_id = str(uuid.uuid4())
    QUEUE_MANAGER.enqueue(
        job_id=job_id, chat_id=chat_id, job_type="publish_ad",
        data={"ad_file": str(ad_file), "chat_id": chat_id},
    )
    return job_id


def _question_text(title: str, questions: list[dict]) -> str:
    lines = []
    for q in questions:
        hint = ", ".join(q["options"][:8]) + (" …" if len(q["options"]) > 8 else "")
        rejected = f" – «{q['rejected']}» gibt es nicht" if q.get("rejected") else ""
        lines.append(f"• {q['label']}{rejected}" + (f"\n  Möglich: {hint}" if hint else ""))
    return (
        f"Kleinanzeigen braucht für «{title}» noch:\n" + "\n".join(lines)
        + "\n\nSchreib mir die Werte, dann schalte ich erneut."
    )


async def answer_publish_question(chat_id: int, text: str) -> bool:
    """Use the user's reply to an open question. Returns False if it was not an answer."""
    entry = _load_questions().get(str(chat_id))
    if not entry:
        return False
    prompt = (
        f"Offene Rückfrage zum Inserat «{entry['title']}»: "
        f"{json.dumps([{'field': q['label'], 'options': q['options'] or 'Freitext'} for q in entry['questions']], ensure_ascii=False)}\n"
        f"Antwort des Nutzers: {json.dumps(text, ensure_ascii=False)}\n"
        "Antworte NUR mit JSON {\"answers\": {Feldname: Wert}}. Werte mit Optionen müssen exakt "
        "einer Option entsprechen. Enthält die Antwort nichts dazu, antworte {\"answers\": {}}."
    )
    parsed = _parse_json_from_response(await _call_agent(chat_id, prompt)) or {}
    answers = parsed.get("answers") if isinstance(parsed.get("answers"), dict) else {}
    if not answers:
        return False
    chosen, still_open = {}, []
    for q in entry["questions"]:
        value = next((v for k, v in answers.items() if str(k).casefold() == q["label"].casefold()), None)
        if value and q["options"]:
            value = next((o for o in q["options"] if o.casefold() == str(value).strip().casefold()), None)
        if value:
            chosen[q["attribute"]] = str(value).strip()
        else:
            still_open.append(q)
    if still_open:
        if chosen:
            entry["questions"] = still_open
            entry.setdefault("answers", {}).update(chosen)
            data = _load_questions()
            data[str(chat_id)] = entry
            _save_questions(data)
        await BOT.send_message(chat_id, _question_text(entry["title"], still_open))
        return True
    chosen = {**entry.get("answers", {}), **chosen}
    ad_file = Path(entry["ad_file"])
    try:
        _edit_special_attributes(ad_file, chosen, [])
    except (OSError, yaml.YAMLError) as e:
        await BOT.send_message(chat_id, f"Fehler beim Speichern der Angaben: {e}")
        return True
    data = _load_questions()
    data.pop(str(chat_id), None)
    _save_questions(data)
    job_id = _enqueue_publish(chat_id, ad_file)
    summary = ", ".join(f"{q['label']}: {chosen[q['attribute']]}" for q in entry["questions"] if q["attribute"] in chosen)
    await BOT.send_message(chat_id, f"Danke. Ich schalte «{entry['title']}» erneut ({summary}). Job {job_id[:8]}")
    return True


async def handle_publish_job(job_data: dict) -> tuple:
    """Publish exactly the ad of this job and confirm it received an ID."""
    chat_id = job_data["chat_id"]
    ad_file = Path(job_data.get("ad_file", ""))
    ad = ad_store.load_ad_file(chat_id, ad_file)
    if not ad:
        return False, "Die Inseratsdatei wurde nicht gefunden."
    if ad.ad_id:  # An earlier attempt already went through.
        return True, f"✅ Anzeige geschaltet: {ad.title} (ID {ad.ad_id})"

    # `publish --ads new` takes every ad without an ID, so park the others.
    parked = [
        other.path
        for other in ad_store.list_local_ads(chat_id)
        if other.active and not other.ad_id and other.path != ad_file
    ]
    for path in parked:
        ad_store.set_active(path, False)
    try:
        result = await bot_runner.publish_new(chat_id)
    except BotFailure as e:
        return False, _describe_failure(e)
    finally:
        for path in parked:
            ad_store.set_active(path, True)

    ad = ad_store.load_ad_file(chat_id, ad_file)
    if ad and ad.ad_id:
        await sync_account(chat_id)
        return True, f"✅ Anzeige geschaltet: {ad.title} (ID {ad.ad_id})"
    log.error("[chat=%d] publish failed (rc=%d):\n%s", chat_id, result.returncode, result.output)
    title = ad.title if ad else ad_file.parent.name
    category = _ad_category(ad_file)
    problems = bot_runner.field_problems(result.output)
    if problems:
        unknown = [p["attribute"] for p in problems if p["kind"] == "unknown"]
        questions = _questions_from_problems(problems, category)
        if category and any(p["kind"] in ("missing", "invalid") for p in problems):
            # Read the fields again: the form may have changed, and the question
            # should offer the current options.
            try:
                category_fields.store(category, await bot_runner.category_fields(chat_id, category))
                questions = _questions_from_problems(problems, category)
            except BotFailure as e:
                log.warning("[chat=%d] refreshing fields of %s failed: %s", chat_id, category, e)
                if "Form rejected" in result.output:
                    category_fields.invalidate(category)
        if unknown:
            _edit_special_attributes(ad_file, {}, unknown)
        if not questions:
            # Only fields the category does not have: drop them and try once more.
            _enqueue_publish(chat_id, ad_file)
            return False, f"«{title}»: {', '.join(unknown)} gibt es in dieser Kategorie nicht. Ich habe es entfernt und schalte erneut.", True
        data = _load_questions()
        data[str(chat_id)] = {"ad_file": str(ad_file), "title": title, "questions": questions}
        _save_questions(data)
        return False, _question_text(title, questions), True
    reasons = bot_runner.failure_reasons(result.output)
    reason = reasons.get(title) or next(iter(reasons.values()), None)
    if "condition_s" in result.output:
        # Next attempt without the attribute this category does not accept.
        _strip_condition(ad_file)
        return False, f"«{title}»: Zustand wird nicht akzeptiert. Ich versuche es ohne Zustand erneut."
    if not reason:
        reason = FAILURE_HINTS.get(bot_runner.classify_failure(result.output), "Details stehen im Log.")
    return (
        False,
        f"Das Schalten von «{title}» ist fehlgeschlagen: {reason}",
        bot_runner.is_permanent_failure(result.output),
    )


def _ad_category(ad_file: Path) -> str | None:
    try:
        value = yaml.safe_load(ad_file.read_text(encoding="utf-8")).get("category")
    except (OSError, AttributeError, yaml.YAMLError):
        return None
    return str(value) if value else None


async def handle_delete_job(job_data: dict) -> tuple[bool, str]:
    """Remove an ad from the site, verify it, and update the local state."""
    chat_id = job_data["chat_id"]
    ad_file = Path(job_data["ad_file"])
    is_sale = bool(job_data.get("sold"))
    ad = ad_store.load_ad_file(chat_id, ad_file)
    if not ad:
        return False, "Die Inseratsdatei wurde nicht gefunden."

    online = None
    if ad.ad_id:
        try:
            online = await bot_runner.list_online(chat_id)
            if any(o.ad_id == ad.ad_id for o in online):
                # The bot ignores inactive ad files.
                ad_store.set_active(ad_file, True)
                try:
                    result = await bot_runner.delete_online(chat_id, ad.ad_id)
                    log.info("[chat=%d] delete output:\n%s", chat_id, result.output)
                    online = await bot_runner.list_online(chat_id)
                finally:
                    ad_store.set_active(ad_file, ad.active)
                if any(o.ad_id == ad.ad_id for o in online):
                    return False, f"«{ad.title}» ist weiterhin online."
        except BotFailure as e:
            return False, _describe_failure(e)

    ad_store.set_active(ad_file, False)
    if is_sale:
        ad_store.record_sold(chat_id, ad)
    if online is not None:
        await _store_online_snapshot(chat_id, online)
    if is_sale:
        return True, f"💰 Als verkauft eingetragen: {ad.title}"
    return True, f"✅ Von Kleinanzeigen gelöscht: {ad.title}"


async def handle_republish_job(job_data: dict) -> tuple[bool, str]:
    """Republish today's due ads, using their current online data."""
    chat_id = job_data["chat_id"]
    try:
        online = await bot_runner.list_online(chat_id)
    except BotFailure as e:
        ad_store.update_health(chat_id, False, e.kind, str(e))
        return False, f"Neu schalten ausgefallen: {_describe_failure(e)}"

    await _import_online_only(chat_id, online)
    selected = ad_store.select_for_republish(
        chat_id, online, KLEINANZEIGEN_REPUBLISH_DAYS
    )
    if not selected:
        await _store_online_snapshot(chat_id, online)
        return True, ""

    lines: list[str] = []
    ready: list[ad_store.LocalAd] = []
    try:
        await bot_runner.download_ids(chat_id, [ad.ad_id for ad in selected])
        for ad in selected:
            downloaded = ad_store.find_download(chat_id, ad.ad_id)
            if not downloaded:
                lines.append(f"⚠️ Übersprungen, Online-Daten fehlen: {ad.title}")
                continue
            try:
                changed = ad_store.merge_online_data(ad.path, downloaded)
            except (OSError, ValueError, yaml.YAMLError) as e:
                log.error("[chat=%d] merge failed for %s: %s", chat_id, ad.path, e)
                lines.append(f"⚠️ Übersprungen, Daten nicht lesbar: {ad.title}")
                continue
            finally:
                shutil.rmtree(downloaded.parent, ignore_errors=True)
            log.info("[chat=%d] Refreshed %s: %s", chat_id, ad.ad_id, changed)
            ready.append(ad)

        if ready:
            result = await bot_runner.publish_ids(chat_id, [ad.ad_id for ad in ready])
            log.info("[chat=%d] republish output:\n%s", chat_id, result.output)
        online = await bot_runner.list_online(chat_id)
    except BotFailure as e:
        return False, f"Neu schalten unterbrochen: {_describe_failure(e)}"

    online_ids = {o.ad_id for o in online}
    reasons = bot_runner.failure_reasons(result.output) if ready else {}
    for ad in ready:
        current = ad_store.load_ad_file(chat_id, ad.path)
        new_id = current.ad_id if current else None
        if not new_id or new_id == ad.ad_id:
            reason = reasons.get(ad.title)
            lines.append(f"❌ Nicht neu geschaltet: {ad.title}" + (f" – {reason}" if reason else ""))
        elif new_id not in online_ids:
            lines.append(f"⚠️ Neue Anzeige online nicht gefunden: {ad.title}")
        elif ad.ad_id in online_ids:
            lines.append(f"⚠️ Jetzt doppelt online, alte Anzeige prüfen: {ad.title}")
        else:
            lines.append(f"🔄 Neu geschaltet: {ad.title}")
    await _store_online_snapshot(chat_id, online)
    return True, "\n".join(lines)


async def handle_sync_job(job_data: dict) -> tuple[bool, str]:
    """Refresh the online snapshot. sync_account reports results itself."""
    await sync_account(job_data["chat_id"], announce=bool(job_data.get("announce")))
    return True, ""


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if q.from_user.id not in ALLOWED_USER_IDS:
        log.warning("Ignored button press from user %s", q.from_user.id)
        return
    chat_id = update.effective_chat.id
    log.info("[chat=%d] button pressed: %r", chat_id, q.data)

    # ── Ad management ───────────────────────────────────────────────────
    if q.data == "nop":
        await q.edit_message_text("Abgebrochen.")
        return
    action, separator, ad_key = q.data.partition(":")
    if separator and action in {"ad", "del", "sold", "deact", "react"}:
        ad = ad_store.find_ad(chat_id, ad_key)
        if not ad:
            await context.bot.send_message(chat_id, "Dieses Inserat gibt es nicht mehr.")
            return

        if action == "ad":
            view = ad_store.online_view(chat_id)
            entry = next((a for a in view["ads"] if a["key"] == ad_key), None)
            if not entry:
                await context.bot.send_message(
                    chat_id, f"«{ad.title}» ist nicht mehr auf Kleinanzeigen."
                )
                return
            expires = f"\nLäuft ab: {entry['expires']}" if entry["expires"] != "-" else ""
            republish = (
                "\nWird automatisch neu geschaltet: "
                f"{'ja' if entry['auto_republish'] else 'nein'}"
                if entry["state"] == "active"
                else ""
            )
            await context.bot.send_message(
                chat_id,
                f"{entry['title']}\nStatus: {_state_label(entry['state'])}{expires}{republish}",
                reply_markup=_ad_actions(entry),
            )
            return

        await q.edit_message_reply_markup(reply_markup=None)
        if action in {"del", "sold"}:
            if not KLEINANZEIGEN_BOT_CMD:
                await context.bot.send_message(
                    chat_id, "⚠️ KLEINANZEIGEN_BOT_CMD ist nicht konfiguriert."
                )
                return
            job_id = str(uuid.uuid4())
            QUEUE_MANAGER.enqueue(
                job_id=job_id,
                chat_id=chat_id,
                job_type="delete_ad",
                data={
                    "ad_file": str(ad.path),
                    "chat_id": chat_id,
                    "sold": action == "sold",
                },
            )
            await context.bot.send_message(
                chat_id, f"⏳ Wird erledigt: {ad.title} (Job {job_id[:8]})"
            )
        else:
            try:
                ad_store.set_active(ad.path, action == "react")
            except OSError as e:
                await context.bot.send_message(chat_id, f"Fehler: {e}")
                return
            text = (
                "▶️ Wird wieder automatisch neu geschaltet."
                if action == "react"
                else "⏸ Wird nicht mehr automatisch neu geschaltet, bleibt aber online."
            )
            await context.bot.send_message(chat_id, f"{text} ({ad.title})")
        return

    # ── Lunch plan confirmation ────────────────────────────────────────────
    if q.data.startswith(("recipe_save:", "recipe_drop:")):
        action, _, proposal_id = q.data.partition(":")
        await q.edit_message_reply_markup(reply_markup=None)
        if action == "recipe_drop":
            recipes.discard_proposal(chat_id, proposal_id)
            await context.bot.send_message(chat_id, "Rezept verworfen.")
            return
        try:
            created = await asyncio.to_thread(recipes.save_proposal, chat_id, proposal_id)
        except Exception as e:
            log.exception("[chat=%d] saving recipe failed", chat_id)
            await context.bot.send_message(
                chat_id,
                f"Fehler beim Speichern: {e}",
                reply_markup=q.message.reply_markup if q.message else None,
            )
            return
        await context.bot.send_message(
            chat_id,
            f"✅ Rezept gespeichert: {created['title']}"
            if created
            else "Dieses Rezept ist nicht mehr offen oder wurde schon bearbeitet.",
        )
        return

    if q.data.startswith("plan_accept:"):
        await q.edit_message_reply_markup(reply_markup=None)
        today = datetime.datetime.now(LUNCH_PLAN_TZ).date()
        try:
            result = await asyncio.to_thread(
                lunch_plan.apply_proposal, q.data.partition(":")[2], today
            )
        except Exception as e:
            log.exception("[chat=%d] plan save failed", chat_id)
            proposal = lunch_plan.load_proposal()
            await context.bot.send_message(
                chat_id,
                f"Fehler beim Speichern: {e}",
                reply_markup=plan_keyboard(proposal["id"]) if proposal else None,
            )
            return
        if result is None:
            await context.bot.send_message(
                chat_id, "Dieser Vorschlag ist nicht mehr offen oder wurde schon bearbeitet."
            )
            return
        lines = [f"✅ {len(result['saved'])} Tage im Essensplan gespeichert."]
        if result["skipped"]:
            days = ", ".join(_weekday_label(i["date"]) for i in result["skipped"])
            lines.append(f"Nicht übernommen, weil schon geplant oder vorbei: {days}")
        for user_id in ALLOWED_USER_IDS:
            await context.bot.send_message(user_id, "\n".join(lines))
        return

    if q.data.startswith("plan_reject:"):
        await q.edit_message_reply_markup(reply_markup=None)
        done = lunch_plan.reject_proposal(q.data.partition(":")[2])
        await context.bot.send_message(
            chat_id, "Vorschlag verworfen." if done else "Dieser Vorschlag ist nicht mehr offen."
        )
        return

    if q.data == "plan_change":
        await q.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_message(
            chat_id,
            "Was soll geändert werden? Schreib mir, welche Tage oder Gerichte du anpassen möchtest.",
        )
        return

    # ── Ad draft buttons ────────────────────────────────────────────────
    d = DRAFTS.get(chat_id)
    if not d:
        await q.edit_message_text("Kein Entwurf mehr aktiv.")
        return

    if q.data == "ok":
        await q.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_message(
            chat_id, render_final(d), parse_mode=ParseMode.MARKDOWN
        )
        await context.bot.send_message(
            chat_id,
            "Öffne jetzt die Kleinanzeigen-App, neues Inserat anlegen, "
            "Fotos anhängen und den Block oben reinkopieren. Viel Erfolg!",
        )
        DRAFTS.pop(chat_id, None)
        CHAT_PHOTO_POOL.pop(chat_id, None)
    elif q.data == "regen":
        await q.edit_message_reply_markup(reply_markup=None)
        await _process_photos(chat_id, d.photos, context)
    elif q.data == "publish":
        if not KLEINANZEIGEN_BOT_CMD:
            await context.bot.send_message(
                chat_id,
                "Direktes Schalten ist nicht konfiguriert (KLEINANZEIGEN_BOT_CMD fehlt).",
            )
            return
        if d.fields_state == "pending":
            await context.bot.send_message(
                chat_id, "Einen Moment, ich ermittle noch die Pflichtfelder der Kategorie."
            )
            return
        if d.missing_fields:
            lines = "\n".join(
                f"• {_field_question(f['label'], category_fields.options_hint(f))}" for f in d.missing_fields
            )
            await context.bot.send_message(
                chat_id, f"Bevor ich schalte, brauche ich noch:\n{lines}\n\nSchreib mir einfach die Werte."
            )
            return
        await q.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
        try:
            ad_file = write_ad_files(d, chat_id)
        except ValueError as e:
            await context.bot.send_message(chat_id, str(e))
            return
        except Exception as e:
            log.exception("Failed to write ad files")
            await context.bot.send_message(chat_id, f"Fehler beim Speichern: {e}")
            return
        job_id = str(uuid.uuid4())
        QUEUE_MANAGER.enqueue(
            job_id=job_id,
            chat_id=chat_id,
            job_type="publish_ad",
            data={"ad_file": str(ad_file), "chat_id": chat_id},
        )
        await context.bot.send_message(
            chat_id,
            f"⏳ Anzeige wird geschaltet…\n_Job-ID: {job_id[:8]}_",
            parse_mode=ParseMode.MARKDOWN,
        )
        log.info("[chat=%d job=%s] Publish job queued", chat_id, job_id)
        DRAFTS.pop(chat_id, None)
        CHAT_PHOTO_POOL.pop(chat_id, None)
        await context.bot.send_message(
            chat_id,
            "Du kannst jetzt das nächste Inserat vorbereiten. "
            "Ich informiere dich, wenn das Schalten abgeschlossen ist.",
        )
    elif q.data == "cancel":
        DRAFTS.pop(chat_id, None)
        CHAT_PHOTO_POOL.pop(chat_id, None)
        _task = ACTIVE_GENERATION_TASKS.pop(chat_id, None)
        if _task and not _task.done():
            _task.cancel()
        await q.edit_message_text("Verworfen.")


def main() -> None:
    global BOT, QUEUE_MANAGER, BACKGROUND_WORKER

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN not set")
    if not ALLOWED_USER_IDS:
        raise SystemExit(
            "ALLOWED_USERS not set – set a comma-separated list of Telegram user IDs"
        )
    log.info("Access restricted to user IDs: %s", ALLOWED_USER_IDS)

    # Initialize queue manager and register its enqueue fn for the tools module
    QUEUE_MANAGER = QueueManager(QUEUE_DIR)
    log.info("Queue manager initialized at %s", QUEUE_DIR)
    set_queue_enqueue_fn(QUEUE_MANAGER.enqueue)

    # Job handlers
    job_handlers = {
        "publish_ad": handle_publish_job,
        "delete_ad": handle_delete_job,
        "sync_ads": handle_sync_job,
        "republish_ads": handle_republish_job,
    }

    app = Application.builder().token(token).build()
    BOT = app.bot

    # Create job completion callback that uses app.bot
    async def notify_job_completion(
        job_id: str, chat_id: int, success: bool, message: str
    ) -> None:
        """Notify user about job completion."""
        if success and not message:
            return
        notification = message if success else f"❌ Fehler: {message}"
        try:
            await app.bot.send_message(
                chat_id,
                notification,
            )
        except Exception as e:
            log.error(
                "[job=%s chat=%d] Failed to send completion notification: %s",
                job_id,
                chat_id,
                e,
            )

    BACKGROUND_WORKER = BackgroundWorker(
        QUEUE_MANAGER,
        job_handlers,
        on_job_completed=notify_job_completion,
    )

    # Register post_init to start the background worker
    async def post_init(app: Application) -> None:
        await BACKGROUND_WORKER.start()
        await app.bot.set_my_commands(BOT_COMMANDS)
        loop = asyncio.get_running_loop()

        def request_confirmation(chat_id: int, action: str, ad: ad_store.LocalAd) -> None:
            """Called from agent threads; the user decides via buttons."""
            question, prefix = (
                ("wirklich als verkauft eintragen", "sold")
                if action == "sold"
                else ("wirklich online löschen", "del")
            )
            asyncio.run_coroutine_threadsafe(
                app.bot.send_message(
                    chat_id,
                    f"«{ad.title}» {question}?",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "✅ Ja", callback_data=f"{prefix}:{ad.key}"
                                ),
                                InlineKeyboardButton("❌ Nein", callback_data="nop"),
                            ]
                        ]
                    ),
                ),
                loop,
            )

        set_confirmation_fn(request_confirmation)
        if KLEINANZEIGEN_BOT_CMD:
            for sync_time in SYNC_TIMES:
                app.job_queue.run_daily(
                    scheduled_sync,
                    time=sync_time.replace(tzinfo=LUNCH_PLAN_TZ),
                    name=f"kleinanzeigen_sync_{sync_time:%H%M}",
                )
        # Schedule daily lunch plan message at 08:30 Europe/Berlin
        app.job_queue.run_daily(
            send_lunch_plan,
            time=datetime.time(8, 30, tzinfo=LUNCH_PLAN_TZ),
            name="lunch_plan_daily",
        )
        log.info("Lunch plan job scheduled at 08:30 Europe/Berlin")
        if not KLEINANZEIGEN_AUTO_REPUBLISH:
            log.info("Kleinanzeigen auto-republish is disabled")
        elif KLEINANZEIGEN_BOT_CMD:
            app.job_queue.run_daily(
                republish_kleinanzeigen,
                time=KLEINANZEIGEN_REPUBLISH_TIME,
                name="kleinanzeigen_republish_daily",
            )
            log.info(
                "Kleinanzeigen republish job scheduled at %s",
                KLEINANZEIGEN_REPUBLISH_TIME.strftime("%H:%M"),
            )

    # Register post_stop to stop the background worker
    async def post_stop(app: Application) -> None:
        await BACKGROUND_WORKER.stop()

    app.post_init = post_init
    app.post_stop = post_stop

    user_filter = filters.User(list(ALLOWED_USER_IDS))

    app.add_handler(CommandHandler("start", cmd_start, filters=user_filter))
    app.add_handler(CommandHandler("help", cmd_start, filters=user_filter))
    app.add_handler(CommandHandler("inserate", cmd_inserate, filters=user_filter))
    app.add_handler(CommandHandler("neu", cmd_neu, filters=user_filter))
    app.add_handler(CommandHandler("lunch", cmd_lunch, filters=user_filter))
    app.add_handler(CommandHandler("woche", cmd_woche, filters=user_filter))
    app.add_handler(CommandHandler("plan", cmd_plan, filters=user_filter))
    app.add_handler(CommandHandler("queue", cmd_queue_status, filters=user_filter))
    app.add_handler(CommandHandler("backout", cmd_backout_jobs, filters=user_filter))
    app.add_handler(
        MessageHandler(filters.Regex(r"^/retry_\S+") & user_filter, cmd_retry_job)
    )
    app.add_handler(MessageHandler(filters.PHOTO & user_filter, on_photo))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & user_filter, on_text)
    )
    app.add_handler(CallbackQueryHandler(on_button, pattern=None))

    # Reject all other users
    async def _unauthorized(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        log.warning(
            "Unauthorized access attempt by user_id=%s",
            update.effective_user and update.effective_user.id,
        )
        if update.message:
            await update.message.reply_text("⛔ Kein Zugriff.")

    app.add_handler(MessageHandler(filters.ALL, _unauthorized))
    log.info("Starting bot…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

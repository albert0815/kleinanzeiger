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

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
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
import mahlino
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
STATUS_LABELS = {
    "online": "🟢 online",
    "reserved": "🟡 reserviert",
    "missing": "⚠️ fehlt online",
    "draft": "📝 nie geschaltet",
    "inactive": "🔴 inaktiv",
    "sold": "💰 verkauft",
    "unknown": "❔ noch nicht geprüft",
}


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


# Mutable per-chat state
DRAFTS: dict[int, Draft] = {}
PENDING_ALBUMS: dict[tuple[int, str], dict] = {}
CHAT_PHOTO_POOL: dict[int, list[bytes]] = {}
ACTIVE_GENERATION_TASKS: dict[int, asyncio.Task] = {}

BOT = None  # set in main(); job handlers have no callback context

# chat_ids that have a pending lunch plan suggestion awaiting confirmation
PENDING_LUNCH_PLAN: set[int] = set()


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


def escape_md(s: str) -> str:
    return s.replace("*", "·").replace("_", " ").replace("`", "'")


def render_draft(d: Draft) -> str:
    missing = ""
    if d.missing_info:
        bullets = "\n".join(f"• {escape_md(m)}" for m in d.missing_info)
        missing = f"\n\n_Noch unklar:_\n{bullets}"
    return (
        f"*{escape_md(d.title)}*\n"
        f"_{escape_md(d.category)} · {escape_md(d.condition)}_\n\n"
        f"{escape_md(d.description)}\n\n"
        f"*Preis:* {d.price_eur} EUR\n"
        f"_{escape_md(d.price_reasoning)}_"
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


def plan_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Annehmen & speichern", callback_data="plan_accept"
                ),
                InlineKeyboardButton("✏️ Ändern", callback_data="plan_change"),
            ]
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
    attributes = f"special_attributes:\n  condition_s: {condition}\n" if condition else ""
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
        await BOT.send_message(
            chat_id,
            "🔄 Erster Online-Abgleich fertig: "
            f"{len(report['missing'])} lokal aktive Inserate fehlen online, "
            f"{len(report['online_only'])} sind nur online vorhanden. "
            "Details unter /inserate.",
        )
        return
    for ad in report["missing"]:
        await BOT.send_message(
            chat_id,
            f"⚠️ Nicht mehr online: {ad['title']}\nWurde es verkauft?",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "💰 Verkauft", callback_data=f"sold:{ad['key']}"
                        ),
                        InlineKeyboardButton(
                            "⏸ Deaktivieren", callback_data=f"deact:{ad['key']}"
                        ),
                    ]
                ]
            ),
        )
    for ad in report["online_only"]:
        await BOT.send_message(chat_id, f"🆕 Nur online vorhanden: {ad['title']}")
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
        cat = recipe.get("category", "")
        duration = recipe.get("duration", "")
        parts = [f"*{name}*"]
        if cat:
            parts.append(escape_md(cat))
        if duration:
            parts.append(f"{duration} min")
        lines.append("• " + " · ".join(parts))
    return "\n".join(lines)


async def _trigger_week_plan(
    user_id: int,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Ask the agent to propose a meal plan for the coming week and present it."""

    trigger = "Bitte erstelle einen Vorschlag für das Mittagessen. "
    try:
        await _call_agent_streaming(
            user_id,
            trigger,
            context,
            reply_markup=plan_keyboard(),
        )
        PENDING_LUNCH_PLAN.add(user_id)
        log.info("[chat=%d] Lunch plan suggestion sent.", user_id)
    except Exception as e:
        log.error("[chat=%d] Failed to send lunch plan suggestion: %s", user_id, e)


async def send_lunch_plan(context: ContextTypes.DEFAULT_TYPE) -> None:
    """JobQueue callback: send today's lunch plan at 08:30.

    If tomorrow has no meal planned, proactively trigger a week plan suggestion.
    """
    today = datetime.datetime.now(LUNCH_PLAN_TZ).date()
    tomorrow = today + datetime.timedelta(days=1)
    log.info("Sending lunch plan for %s to %d user(s)", today, len(ALLOWED_USER_IDS))

    try:
        plan_range = await mahlino.fetch_plan(today, tomorrow)
    except Exception as e:
        log.error("Failed to fetch lunch plan: %s", e)
        return

    today_msg = format_lunch_message(plan_range, today)
    tomorrow_has_meal = _has_meal(plan_range, tomorrow)

    for user_id in ALLOWED_USER_IDS:
        try:
            await context.bot.send_message(
                user_id, today_msg, parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            log.error("Failed to send today's lunch to user %d: %s", user_id, e)
            continue

        if not tomorrow_has_meal:
            await _trigger_week_plan(user_id, context)


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
    await update.message.reply_text(
        "Hallo! Ich kann zwei Dinge:\n\n"
        "📸 *Kleinanzeigen-Inserate* — Schick mir ein Foto (oder mehrere als Album) "
        "von dem Gegenstand, den du verkaufen willst.\n\n"
        "🍽️ *Mittagessen-Planung* — Jeden Morgen um 08:30 schicke ich dir den "
        "aktuellen Essensplan. Falls für die nächste Woche noch etwas fehlt, "
        "schlage ich einen Plan vor.\n\n"
        "Befehle:\n"
        "/inserate — Aktive Inserate verwalten\n"
        "/lunch — Heutiges Mittagessen anzeigen\n"
        "/plan — Essensplan für nächste Woche vorschlagen\n"
        "/neu — Konversation zurücksetzen\n"
        "/queue — Warteschlangen-Status",
        parse_mode=ParseMode.MARKDOWN,
    )


def _sync_is_stale(chat_id: int) -> bool:
    synced_at = ad_store.load_state(chat_id)["synced_at"]
    if not synced_at:
        return True
    age = datetime.datetime.now(datetime.UTC) - datetime.datetime.fromisoformat(synced_at)
    return age > SYNC_MAX_AGE


def _render_ad_list(chat_id: int) -> tuple[str, InlineKeyboardMarkup | None]:
    view = ad_store.overview(chat_id)
    ads = [a for a in view["ads"] if a["status"] != "sold"]
    counts = [
        f"{label}: {n}"
        for status, label in STATUS_LABELS.items()
        if (n := sum(a["status"] == status for a in view["ads"]))
    ]
    if view["synced_at"]:
        checked = (
            datetime.datetime.fromisoformat(view["synced_at"])
            .astimezone(LUNCH_PLAN_TZ)
            .strftime("%d.%m. %H:%M")
        )
        lines = [f"Stand online: {checked}"]
    else:
        lines = ["Noch kein Online-Abgleich erfolgreich."]
    if not view["health"]["ok"]:
        lines.append("⚠️ Der letzte Abgleich ist fehlgeschlagen, der Stand kann veraltet sein.")
    lines.extend(counts)
    if view["online_only"]:
        lines.append("\nNur online vorhanden:")
        lines.extend(f"• {a['title']}" for a in view["online_only"])
    order = list(STATUS_LABELS).index
    priority = {"missing": 0, "unknown": 1, "draft": 2}
    ads.sort(key=lambda a: (priority.get(a["status"], 3), order(a["status"]), a["title"]))
    # Telegram allows at most 100 inline buttons per message.
    rows = [
        [
            InlineKeyboardButton(
                f"{STATUS_LABELS[a['status']].split()[0]} {a['title'][:45]}",
                callback_data=f"ad:{a['key']}",
            )
        ]
        for a in ads[:100]
    ]
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
    """Show all ads with their online status; tapping one opens its actions."""
    chat_id = update.effective_chat.id
    if not ad_store.list_local_ads(chat_id) and not KLEINANZEIGEN_BOT_CMD:
        await update.message.reply_text("Keine lokalen Inserate gefunden.")
        return
    if _sync_is_stale(chat_id):
        await update.message.reply_text("🔍 Prüfe den Stand auf Kleinanzeigen…")
    # A browser run takes a while, so don't block other incoming updates.
    context.application.create_task(_show_ad_list(chat_id))


def _ad_actions(entry: dict) -> InlineKeyboardMarkup | None:
    key, status = entry["key"], entry["status"]
    sold = InlineKeyboardButton("💰 Verkauft", callback_data=f"sold:{key}")
    delete = InlineKeyboardButton("🗑 Online löschen", callback_data=f"del:{key}")
    deactivate = InlineKeyboardButton("⏸ Lokal deaktivieren", callback_data=f"deact:{key}")
    reactivate = InlineKeyboardButton("▶️ Lokal aktivieren", callback_data=f"react:{key}")
    match status:
        case "online" | "reserved":
            rows = [[sold, delete]]
            rows.append([deactivate if entry["local_active"] else reactivate])
        case "missing" | "unknown":
            rows = [[sold, deactivate]]
        case "draft":
            rows = [[deactivate]]
        case "inactive":
            rows = [[reactivate]]
        case _:
            return None
    return InlineKeyboardMarkup(rows)


async def cmd_neu(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    DRAFTS.pop(chat_id, None)
    CHAT_PHOTO_POOL.pop(chat_id, None)
    task = ACTIVE_GENERATION_TASKS.pop(chat_id, None)
    if task and not task.done():
        task.cancel()
    PENDING_LUNCH_PLAN.discard(chat_id)
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
    try:
        if d:
            response = await _call_agent(chat_id, user_request)
        else:
            markup = plan_keyboard() if chat_id in PENDING_LUNCH_PLAN else None
            response = await _call_agent_streaming(
                chat_id,
                user_request,
                context,
                reply_markup=markup,
            )
    except Exception as e:
        log.exception("[chat=%d] agent call failed", chat_id)
        await context.bot.send_message(chat_id, f"Fehler: {e}")
        return

    # If there is an active draft and the response is JSON, update it
    if d:
        parsed = _parse_json_from_response(response)
        if parsed and "title" in parsed:
            _apply_dict_to_draft(d, parsed)
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
    await context.bot.send_message(
        chat_id,
        render_draft(d),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ad_keyboard(),
    )


async def handle_publish_job(job_data: dict) -> tuple[bool, str]:
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
    if "condition_s" in result.output:
        # Next attempt without the attribute this category does not accept.
        _strip_condition(ad_file)
    kind = bot_runner.classify_failure(result.output)
    hint = FAILURE_HINTS.get(kind, "Details stehen im Log.")
    return False, f"Das Schalten ist fehlgeschlagen: {hint}"


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
    return True, f"✅ Nicht mehr online und lokal deaktiviert: {ad.title}"


async def handle_republish_job(job_data: dict) -> tuple[bool, str]:
    """Republish today's due ads, using their current online data."""
    chat_id = job_data["chat_id"]
    try:
        online = await bot_runner.list_online(chat_id)
    except BotFailure as e:
        ad_store.update_health(chat_id, False, e.kind, str(e))
        return False, f"Neu schalten ausgefallen: {_describe_failure(e)}"

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
    for ad in ready:
        current = ad_store.load_ad_file(chat_id, ad.path)
        new_id = current.ad_id if current else None
        if not new_id or new_id == ad.ad_id:
            lines.append(f"❌ Nicht neu geschaltet: {ad.title}")
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
            view = ad_store.overview(chat_id)
            entry = next(a for a in view["ads"] if a["key"] == ad_key)
            expires = f"\nLäuft ab: {entry['expires']}" if entry["expires"] else ""
            await context.bot.send_message(
                chat_id,
                f"{ad.title}\nPreis: {ad.price} EUR\n"
                f"Status: {STATUS_LABELS[entry['status']]}{expires}",
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
                "▶️ Lokal aktiviert."
                if action == "react"
                else "⏸ Lokal deaktiviert. Online ändert das nichts."
            )
            await context.bot.send_message(chat_id, f"{text} ({ad.title})")
        return

    # ── Lunch plan confirmation ────────────────────────────────────────────
    if q.data == "plan_accept":
        agent = get_agent(chat_id)
        agent.state["plan_confirmed"] = True
        PENDING_LUNCH_PLAN.discard(chat_id)
        await q.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
        try:
            await _call_agent_streaming(
                chat_id,
                "Bitte speichere den Plan jetzt.",
                context,
            )
        except Exception as e:
            log.exception("[chat=%d] plan save failed", chat_id)
            await context.bot.send_message(chat_id, f"Fehler beim Speichern: {e}")
        finally:
            agent.state["plan_confirmed"] = False
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

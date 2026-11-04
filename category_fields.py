"""Category-specific ad fields, read with the bot's `fields` command and cached.

Kleinanzeigen asks for different fields per category (e.g. "Typ" for bicycle
accessories, "Größe" for children's shoes). Missing required fields make the
site reject the ad, so drafts are completed with these fields before publishing.
"""

from __future__ import annotations

import datetime
import json
import logging
import threading
from pathlib import Path

import ad_store

log = logging.getLogger("kleinanzeigen-agent.fields")

MAX_AGE = datetime.timedelta(days=30)
# Handled elsewhere: condition by the bot's condition dialog, shipping by the ad config.
SKIPPED = {"condition_s", "versand_s"}
_LOCK = threading.Lock()


def _path() -> Path:
    return ad_store.base_dir() / ".category-fields.json"


def _load() -> dict:
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log.error("Cannot read category fields: %s", e)
        return {}


def _save(data: dict) -> None:
    temporary = _path().with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    temporary.replace(_path())


def cached(category: str, now: datetime.datetime | None = None) -> list[dict] | None:
    now = now or datetime.datetime.now(datetime.UTC)
    entry = _load().get(category)
    if not entry or now - datetime.datetime.fromisoformat(entry["fetched_at"]) > MAX_AGE:
        return None
    return entry["fields"]


def store(category: str, fields: list[dict]) -> None:
    with _LOCK:
        data = _load()
        data[category] = {
            "fetched_at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
            "fields": fields,
        }
        _save(data)


def invalidate(category: str) -> None:
    with _LOCK:
        data = _load()
        if data.pop(category, None) is not None:
            _save(data)


def to_decide(fields: list[dict]) -> list[dict]:
    """Fields a draft has to fill: not preset by the category, not handled elsewhere."""
    return [f for f in fields if f["attribute"] not in SKIPPED and not f.get("preset")]


def match_option(field: dict, value) -> str | None:
    """The stored value for an option given by its label or value."""
    wanted = " ".join(str(value).split()).casefold()
    for option in field["options"]:
        if wanted in (option["label"].casefold(), str(option["value"]).casefold()):
            return option["value"] or option["label"]
    return None


def option_label(field: dict, value: str) -> str:
    return next((o["label"] for o in field["options"] if (o["value"] or o["label"]) == value), value)


def find_field(fields: list[dict], name) -> dict | None:
    wanted = str(name).strip().casefold()
    return next(
        (f for f in fields if wanted in (f["label"].casefold(), f["attribute"].casefold(), f["key"].casefold())),
        None,
    )


def validate(fields: list[dict], attributes: dict) -> tuple[dict[str, str], list[dict]]:
    """Map the agent's choices to stored values. Returns (chosen, missing required).

    Keys may be the field label, the attribute or the form key. Values must be one
    of the offered options (label or value, case-insensitive); others are dropped.
    """
    chosen: dict[str, str] = {}
    open_fields = to_decide(fields)
    for name, value in (attributes or {}).items():
        field = find_field(open_fields, name)
        if not field or value in (None, ""):
            continue
        if field["options"]:
            stored = match_option(field, value)
            if stored:
                chosen[field["attribute"]] = stored
        else:
            chosen[field["attribute"]] = " ".join(str(value).split())[:100]
    missing = [f for f in open_fields if f["required"] and f["attribute"] not in chosen]
    return chosen, missing


def describe_for_agent(fields: list[dict]) -> list[dict]:
    return [
        {
            "field": f["label"],
            "required": f["required"],
            "options": [o["label"] for o in f["options"]][:150] or "Freitext",
        }
        for f in to_decide(fields)
    ]


def options_hint(field: dict, limit: int = 6) -> str:
    labels = [o["label"] for o in field["options"]]
    if not labels:
        return ""
    return ", ".join(labels[:limit]) + (" …" if len(labels) > limit else "")

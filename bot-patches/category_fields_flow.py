# Added by kleinanzeiger: `kleinanzeigen-bot fields --category=<name or id>`.
"""List the category-specific fields of the ad form, without submitting anything."""

from __future__ import annotations

import json
from typing import Any

from . import publishing_form
from .utils import loggers as _loggers
from .utils import page_scripts
from .utils.web_scraping_mixin import WebScrapingMixin

LOG = _loggers.get_logger(__name__)
OUTPUT_PREFIX = "CATEGORY_FIELDS_JSON "


async def read_category_fields(
    web:WebScrapingMixin, root_url:str, category:str, categories:dict[str, str]
) -> list[dict[str, Any]]:
    """Open the ad form, select the category, and describe its attribute fields.

    Each field has ``key`` (e.g. ``fahrraeder.type``), ``attribute`` (the
    ``special_attributes`` key, e.g. ``type_s``), ``label``, ``required``, ``preset``
    (the value the category preselects) and ``options`` (``value`` and ``label``).
    """
    category_id = categories.get(category, category)
    await web.web_open(f"{root_url}/p-anzeige-aufgeben-schritt2.html", reload_if_already_open = True)
    await web.dismiss_consent_banner()
    await publishing_form.set_category(web, category = category_id, ad_file = "(fields)")
    await web.web_sleep()  # category-dependent fields render after the category is set
    fields = await web.web_execute(page_scripts.call("category_fields"))
    return fields if isinstance(fields, list) else []


def render(category:str, fields:list[dict[str, Any]]) -> str:
    return OUTPUT_PREFIX + json.dumps({"category": category, "fields": fields}, ensure_ascii = False)

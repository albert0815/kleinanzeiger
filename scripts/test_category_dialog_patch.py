"""Checks scripts/patch_category_dialog_labels.py inside the kleinanzeigen-bot build.

In the redesigned dialog the selection text of a choice is only the sr-only
suffix; the real name has to come from web_apply. Without the patch this fails
with "Category dialog has no unique match", like the live run on 2026-11-03.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

from kleinanzeigen_bot import publishing_form

CHOICES = {
    0: ["Auto, Rad & Boot", "Familie, Kind & Baby", "Mode & Beauty"],
    1: ["Alle Kategorien", "Altenpflege", "Baby- & Kinderschuhe", "Baby-Ausstattung"],
}


def test_selects_leaf_with_real_names():
    level = {"n": 0}
    pressed = {}

    def make(name):
        button = MagicMock()
        button.name = name

        async def click():
            if level["n"] == 1:
                pressed["leaf"] = button
            level["n"] = min(level["n"] + 1, 1) if name == "Familie, Kind & Baby" else level["n"]
        button.click = click
        return button

    apply_button = MagicMock()
    apply_button.name = "Übernehmen"
    apply_button.click = AsyncMock()

    async def find_all(_by, selector, **_):
        if selector.endswith('[aria-pressed="true"]'):
            return [pressed["leaf"]]
        if selector.endswith("li > button"):
            return [make(n) for n in CHOICES[level["n"]]]
        return [apply_button]

    web = MagicMock()
    web.categories = {"Familie, Kind & Baby > Baby-Ausstattung": "17/258"}
    web.timeout = lambda *_: 1
    web.web_sleep = AsyncMock()
    web.web_find_all = find_all
    web.web_apply = AsyncMock(side_effect=lambda el, _js: el.name)
    # What headless Chromium returns for the choice buttons; the apply button reads fine.
    web.extract_visible_text = AsyncMock(
        side_effect=lambda el: el.name if el is apply_button else ", öffnet Unterkategorien"
    )
    # The dialog is closed after applying.
    publishing_form._probe_category_dialog = AsyncMock(return_value=None)
    asyncio.run(publishing_form._set_category_in_dialog(web, category="17/258", dialog_selector="dialog[open]"))
    assert pressed["leaf"].name == "Baby-Ausstattung"
    apply_button.click.assert_awaited_once()

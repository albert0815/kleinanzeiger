#!/usr/bin/env python3
"""Patch kleinanzeigen-bot to read the labels of the redesigned category dialog.

Since early November 2026 the category dialog renders each choice as
``<button><span aria-hidden>icon</span><span>Name</span><span class="sr-only">,
öffnet Unterkategorien</span></button>``. The bot reads labels through a text
selection (``visible_text.js``). In headless Chromium that selection returns
an empty string or only the screen-reader suffix for these buttons, so no
choice matches and every publish fails with
"Category dialog has no unique match … Offered choices: , öffnet Unterkategorien".

This patch reads those buttons via ``textContent`` without screen-reader-only
and ``aria-hidden`` parts, falling back to the original method. Only the
category choice buttons are affected; other texts keep the original method.

Run from the kleinanzeigen-bot source root before ``pdm run compile``. Fails
loudly if upstream changed the patched code, so a rebuild cannot silently
drop the fix. Idempotent.
"""

from __future__ import annotations

import sys
from pathlib import Path

TARGET = Path("src/kleinanzeigen_bot/publishing_form.py")
MARKER = "# kleinanzeiger patch: category dialog labels"

HELPER = '''
{marker}
_CATEGORY_CHOICE_LABEL_JS = """(elem) => {{
  const copy = elem.cloneNode(true);
  copy.querySelectorAll('.sr-only, [aria-hidden="true"]').forEach((part) => part.remove());
  return (copy.textContent || '').replace(/\\\\s+/g, ' ').trim();
}}"""


async def _category_choice_label(web:WebScrapingMixin, button:Element) -> str:
    """Label of a category dialog choice, without icons and screen-reader text."""
    label = str(await web.web_apply(button, _CATEGORY_CHOICE_LABEL_JS) or "").strip()
    return label or await web.extract_visible_text(button)


async def _click_category_dialog_level('''.format(marker=MARKER)

REPLACEMENTS = [
    ("\nasync def _click_category_dialog_level(", HELPER),
    (
        "            label = await web.extract_visible_text(button)\n            if label:\n                score = max(",
        "            label = await _category_choice_label(web, button)\n            if label:\n                score = max(",
    ),
    (
        "    selected_labels = {await web.extract_visible_text(button) for button in selected_buttons}",
        "    selected_labels = {await _category_choice_label(web, button) for button in selected_buttons}",
    ),
]


def main() -> int:
    text = TARGET.read_text(encoding="utf-8")
    if MARKER in text:
        print(f"{TARGET}: already patched")
        return 0
    for old, new in REPLACEMENTS:
        if text.count(old) != 1:
            print(f"{TARGET}: expected code not found exactly once, upstream changed:\n{old}", file=sys.stderr)
            return 1
        text = text.replace(old, new)
    TARGET.write_text(text, encoding="utf-8")
    print(f"{TARGET}: patched category dialog label reading")
    return 0


if __name__ == "__main__":
    sys.exit(main())

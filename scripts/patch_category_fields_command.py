#!/usr/bin/env python3
"""Add a ``fields`` command to kleinanzeigen-bot and name fields in form errors.

1. ``kleinanzeigen-bot fields --category=<name or id>`` logs in, opens the ad
   form, selects the category with the bot's own category dialog code and prints
   the category's attribute fields as one JSON line (``CATEGORY_FIELDS_JSON {...}``):
   key, special_attributes key, label, required, preset value and options. It
   never submits the form.
2. Form validation errors name the field key, e.g.
   ``Typ [fahrraeder.type]: Bitte gib einen Wert ein``, so a missing value can be
   written to the right ``special_attributes`` key.

Run from the kleinanzeigen-bot source root before ``pdm run compile``, with the
files from ``bot-patches/`` next to this script. Fails loudly if upstream changed
the patched code. Idempotent.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

PATCHES = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("bot-patches")
SRC = Path("src/kleinanzeigen_bot")
MARKER = "kleinanzeiger patch: fields command"

EDITS = {
    SRC / "runtime_config.py": [
        ('    "reserve", "activate", "login",\n})',
         '    "reserve", "activate", "login",\n    "fields",  # kleinanzeiger patch: fields command\n})'),
    ],
    SRC / "cli.py": [
        ("    workspace_mode:str | None = None\n",
         "    workspace_mode:str | None = None\n    category_arg:str | None = None  # kleinanzeiger patch: fields command\n"),
        ('["ads=", "config=", "force",', '["ads=", "category=", "config=", "force",'),
        ('            case "--force":\n',
         '            case "--category":  # kleinanzeiger patch: fields command\n'
         '                parsed.category_arg = value.strip()\n'
         '            case "--force":\n'),
    ],
    SRC / "app.py": [
        ("        self.keep_old_ads = parsed.keep_old_ads\n",
         "        self.keep_old_ads = parsed.keep_old_ads\n"
         "        self._category_arg = parsed.category_arg  # kleinanzeiger patch: fields command\n"),
        ('                case "list":\n                    await self._handle_list()\n',
         '                case "list":\n                    await self._handle_list()\n'
         '                case "fields":  # kleinanzeiger patch: fields command\n'
         '                    await self._handle_fields()\n'),
        ("    async def _handle_publish(self) -> None:\n",
         "    async def _handle_fields(self) -> None:  # kleinanzeiger patch: fields command\n"
         '        """Print the attribute fields of one category as JSON, without submitting."""\n'
         "        from . import category_fields_flow\n"
         "        if not self._category_arg:\n"
         '            LOG.error("Missing --category=<name or id>.")\n'
         "            sys.exit(2)\n"
         "        self._bootstrap_runtime()\n"
         "        await self._open_logged_in_browser()\n"
         "        fields = await category_fields_flow.read_category_fields(\n"
         "            self, self.root_url, self._category_arg, self.categories\n"
         "        )\n"
         "        print(category_fields_flow.render(self._category_arg, fields), flush = True)\n"
         "\n"
         "    async def _handle_publish(self) -> None:\n"),
    ],
    SRC / "resources/js/form_validation_errors.js": [
        ("    const errors = new Set();\n",
         "    // kleinanzeiger patch: fields command - name the field key next to its label.\n"
         "    /** @param {Element} element */\n"
         "    const keyFor = (element) => {\n"
         "      /** @type {Element | null} */\n"
         "      let scope = element;\n"
         "      for (let depth = 0; depth < 6 && scope && scope !== form; depth++, scope = scope.parentElement) {\n"
         "        const keys = new Set();\n"
         "        for (const control of [scope, ...scope.querySelectorAll(\"[id], [name]\")]) {\n"
         "          const name = control.getAttribute(\"name\") || \"\";\n"
         "          if (name.startsWith(\"attributeMap[\")) keys.add(name.slice(13, -1));\n"
         "          else if (control.id && /^[a-z0-9_]+\\.[a-z0-9_]+$/.test(control.id)) keys.add(control.id);\n"
         "        }\n"
         "        if (keys.size === 1) return [...keys][0];\n"
         "        if (keys.size > 1) return \"\";\n"
         "      }\n"
         "      return \"\";\n"
         "    };\n"
         "    const errors = new Set();\n"),
        ("      const label = labelFor(element);\n      errors.add(label ? `${label}: ${message}` : message);\n",
         "      const label = labelFor(element);\n"
         "      const key = keyFor(element);\n"
         "      const name = label && key ? `${label} [${key}]` : label;\n"
         "      errors.add(name ? `${name}: ${message}` : message);\n"),
    ],
}


def main() -> int:
    if MARKER in (SRC / "app.py").read_text(encoding = "utf-8"):
        print("fields command: already patched")
        return 0
    for path, edits in EDITS.items():
        text = path.read_text(encoding = "utf-8")
        for old, new in edits:
            if text.count(old) != 1:
                print(f"{path}: expected code not found exactly once, upstream changed:\n{old}", file = sys.stderr)
                return 1
            text = text.replace(old, new)
        path.write_text(text, encoding = "utf-8")
    shutil.copy(PATCHES / "category_fields_flow.py", SRC / "category_fields_flow.py")
    shutil.copy(PATCHES / "category_fields.js", SRC / "resources/js/category_fields.js")
    print("fields command: patched")
    return 0


if __name__ == "__main__":
    sys.exit(main())

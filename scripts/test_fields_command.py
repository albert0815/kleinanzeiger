"""Checks scripts/patch_category_fields_command.py inside the kleinanzeigen-bot build."""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from kleinanzeigen_bot import category_fields_flow, cli, runtime_config
from kleinanzeigen_bot.utils import page_scripts


def test_command_and_option_are_known():
    assert "fields" in runtime_config.VALID_COMMANDS
    parsed = cli.parse_args(["kleinanzeigen-bot", "fields", "--category=210/217/zubehoer"])
    assert (parsed.command, parsed.category_arg) == ("fields", "210/217/zubehoer")


def test_flow_selects_resolved_category_and_never_submits():
    web = MagicMock()
    web.web_open = AsyncMock()
    web.dismiss_consent_banner = AsyncMock()
    web.web_sleep = AsyncMock()
    fields = [{"key": "fahrraeder.type", "attribute": "type_s", "label": "Typ", "required": True, "preset": "", "options": []}]
    web.web_execute = AsyncMock(return_value=fields)
    with patch("kleinanzeigen_bot.publishing_form.set_category", new=AsyncMock()) as set_category:
        result = asyncio.run(category_fields_flow.read_category_fields(
            web, "https://www.kleinanzeigen.de", "Auto, Rad & Boot > Fahrräder & Zubehör > Zubehör",
            {"Auto, Rad & Boot > Fahrräder & Zubehör > Zubehör": "210/217/zubehoer"},
        ))
    assert result == fields
    assert set_category.await_args.kwargs["category"] == "210/217/zubehoer"
    executed = " ".join(str(c.args[0]) for c in web.web_execute.await_args_list)
    assert "submit" not in executed.lower()
    assert category_fields_flow.render("x", fields).startswith(category_fields_flow.OUTPUT_PREFIX)


def test_page_scripts_load():
    assert "attributeMap" in page_scripts.call("category_fields")
    assert "keyFor" in page_scripts.call("form_validation_errors")

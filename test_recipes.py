"""Tests for capturing recipes. Network and Mahlino are replaced."""

import asyncio
import json
import os
import socket
import tempfile
import unittest
from unittest import mock

import httpx

import mahlino
import recipes

INGREDIENTS = [{"text": "Mehl", "quantity": 500, "unit": "g"}, {"text": "Salz"}]
PAGE = """<html><head><script type="application/ld+json">
{"@graph":[{"@type":"WebSite"},{"@type":["Recipe"],"name":"Brot &amp; Butter","recipeIngredient":["500 g Mehl"],
"prepTime":"PT10M","image":"x.jpg"}]}</script></head><body>Text</body></html>"""


def recipe(**changes):
    values = {
        "title": "Brot", "ingredients": INGREDIENTS, "instructions": "Backen", "servings": 2,
        "prep_minutes": 10, "cook_minutes": 40, "description": None, "source_url": None,
    }
    return recipes.build_recipe(**{**values, **changes})


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"KLEINANZEIGEN_BASE_DIR": tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)


def resolves_to(address):
    return mock.patch.object(
        socket, "getaddrinfo", return_value=[(2, 1, 6, "", (address, 0))]
    )


class UrlCheckTest(unittest.TestCase):
    def test_blocks_non_public_targets(self):
        for address in ("127.0.0.1", "192.168.178.1", "10.0.0.5", "169.254.169.254", "::1"):
            with resolves_to(address), self.assertRaises(recipes.RecipeError):
                recipes.check_url("https://rezept.example/x")

    def test_blocks_bad_urls(self):
        for url in ("ftp://a.example/", "file:///etc/passwd", "http://u:p@a.example/", "http://a.example:8000/", "kein link"):
            with resolves_to("93.184.216.34"), self.assertRaises(recipes.RecipeError):
                recipes.check_url(url)

    def test_allows_public(self):
        with resolves_to("93.184.216.34"):
            recipes.check_url("https://rezept.example/x")


class FakeTransportMixin:
    def client(self, handler):
        real = httpx.Client

        def build(**kwargs):
            return real(transport=httpx.MockTransport(handler), **kwargs)

        return mock.patch.object(httpx, "Client", build)


class ReadPageTest(FakeTransportMixin, unittest.TestCase):
    def test_structured_recipe_is_found_and_trimmed(self):
        found = recipes.extract_structured_recipe(PAGE)
        self.assertEqual(found["name"], "Brot & Butter")
        self.assertNotIn("image", found)

    def test_text_fallback_skips_scripts(self):
        text = recipes.page_text("<body><script>evil()</script><p>Zutaten</p></body>")
        self.assertEqual(text, "Zutaten")

    def test_redirect_to_home_network_is_blocked(self):
        def handler(request):
            return httpx.Response(302, headers={"location": "http://fritz.box/"})

        def lookup(host, *_):
            address = "192.168.178.1" if host == "fritz.box" else "93.184.216.34"
            return [(2, 1, 6, "", (address, 0))]

        with self.client(handler), mock.patch.object(socket, "getaddrinfo", lookup):
            with self.assertRaises(recipes.RecipeError):
                recipes.read_recipe_page("https://rezept.example/x")

    def test_oversized_page_is_refused(self):
        def handler(request):
            return httpx.Response(200, headers={"content-type": "text/html"}, content=b"x" * (recipes.MAX_PAGE_BYTES + 1))

        with self.client(handler), resolves_to("93.184.216.34"):
            with self.assertRaises(recipes.RecipeError):
                recipes.read_recipe_page("https://rezept.example/x")

    def test_reads_page(self):
        def handler(request):
            return httpx.Response(200, headers={"content-type": "text/html"}, text=PAGE)

        with self.client(handler), resolves_to("93.184.216.34"):
            result = recipes.read_recipe_page(" https://rezept.example/x ")
        self.assertEqual(result["source_url"], "https://rezept.example/x")
        self.assertIsNone(result["page_text"])


class BuildTest(unittest.TestCase):
    def test_valid_and_normalized(self):
        result = recipe(ingredients=[{"text": " Mehl ", "quantity": "500", "unit": "g"}, {"text": "Salz", "quantity": 0}])
        self.assertEqual(result["ingredients"][0]["quantity"], 500.0)
        self.assertEqual(result["ingredients"][0]["text"], "Mehl")
        self.assertIsNone(result["ingredients"][1]["quantity"])

    def test_invalid(self):
        for changes in ({"title": ""}, {"ingredients": []}, {"ingredients": [{"quantity": 1}]},
                        {"prep_minutes": -1}, {"source_url": "javascript:x"}):
            with self.assertRaises(recipes.RecipeError):
                recipe(**changes)

    def test_payload_matches_mahlino_fields(self):
        self.assertEqual(
            set(recipe()),
            {"title", "description", "servings", "prep_minutes", "cook_minutes", "instructions", "source_url", "ingredients"},
        )


class PanelRulesTest(unittest.TestCase):
    """Rules from RecipeScreen.kt in the kitchen panel."""

    def test_lines_become_clean_steps(self):
        steps, link = recipes.clean_instructions(
            "1. **Schälen**\n\n2) Kochen\n- Salzen\n• Rühren\n## Für die Soße:\nTipp: Heiß servieren\n"
        )
        self.assertEqual(
            steps, "Schälen\nKochen\nSalzen\nRühren\nFür die Soße:\nTipp: Heiß servieren"
        )
        self.assertIsNone(link)

    def test_link_moves_to_source(self):
        result = recipe(instructions="https://rezept.example/x")
        self.assertIsNone(result["instructions"])
        self.assertEqual(result["source_url"], "https://rezept.example/x")

    def test_link_between_steps_is_removed_and_given_source_wins(self):
        result = recipe(instructions="Kochen\nhttps://b.example/\nEssen", source_url="https://a.example/")
        self.assertEqual(result["instructions"], "Kochen\nEssen")
        self.assertEqual(result["source_url"], "https://a.example/")

    def test_preview_matches_panel(self):
        steps = recipes.format_steps(
            "Für die Soße:\nZwiebeln schneiden\nTipp: Heiß servieren\nAnbraten\n"
            "Diese sehr lange Zeile endet mit Doppelpunkt und ist keine Überschrift:"
        )
        self.assertEqual(
            steps,
            [
                "\nFür die Soße",
                "1. Zwiebeln schneiden",
                "💡 Tipp: Heiß servieren",
                "2. Anbraten",
                "3. Diese sehr lange Zeile endet mit Doppelpunkt und ist keine Überschrift:",
            ],
        )

    def test_description_is_one_paragraph(self):
        self.assertEqual(recipe(description="Lecker\n\n  und   schnell")["description"], "Lecker und schnell")


class ProposalTest(Base):
    def test_saved_once(self):
        proposal = recipes.store_proposal(1, recipe())
        create = mock.Mock(return_value={"title": "Brot"})
        with mock.patch.object(mahlino, "create_recipe_sync", create):
            self.assertEqual(recipes.save_proposal(1, proposal["id"])["title"], "Brot")
            self.assertIsNone(recipes.save_proposal(1, proposal["id"]))
        create.assert_called_once()

    def test_other_chat_cannot_save(self):
        proposal = recipes.store_proposal(1, recipe())
        self.assertIsNone(recipes.save_proposal(2, proposal["id"]))

    def test_failure_keeps_proposal_for_retry(self):
        proposal = recipes.store_proposal(1, recipe())
        with mock.patch.object(mahlino, "create_recipe_sync", side_effect=OSError("weg")):
            with self.assertRaises(OSError):
                recipes.save_proposal(1, proposal["id"])
        create = mock.Mock(return_value={"title": "Brot"})
        with mock.patch.object(mahlino, "create_recipe_sync", create):
            self.assertIsNotNone(recipes.save_proposal(1, proposal["id"]))

    def test_new_proposal_replaces_old_and_is_announced_once(self):
        old = recipes.store_proposal(1, recipe())
        new = recipes.store_proposal(1, recipe(title="Neu"))
        self.assertEqual(recipes.take_unannounced(1)["id"], new["id"])
        self.assertIsNone(recipes.take_unannounced(1))
        self.assertFalse(recipes.discard_proposal(1, old["id"]))

    def test_long_recipe_fits_telegram(self):
        text = recipes.format_proposal(recipe(instructions="x" * 9000))
        self.assertLessEqual(len(text), 4096)
        self.assertIn("gekürzt", text)
        self.assertIn("500 g Mehl", text)


class ToolAndBotTest(Base):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"ALLOWED_USERS": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        import main
        import tools

        self.main, self.tools = main, tools
        main.BOT = mock.AsyncMock()
        users = mock.patch.object(main, "ALLOWED_USER_IDS", {1})
        users.start()
        self.addCleanup(users.stop)
        tools.set_agent_chat_id(1)

    def propose(self, **extra):
        existing = [{"id": "brot", "name": "BROT", "lastPlanDate": None, "minutes": None}]
        with mock.patch.object(mahlino, "fetch_recipes_sync", return_value=existing):
            return self.tools.propose_recipe(title="Brot", ingredients=INGREDIENTS, **extra)

    def test_duplicate_needs_confirmation(self):
        self.assertIn("gibt es schon", self.propose())
        self.assertIn("angelegt", self.propose(allow_duplicate=True))

    def test_button_saves_without_agent(self):
        proposal = recipes.store_proposal(1, recipe())
        asyncio.run(self.main.announce_recipe_proposal(1))
        markup = self.main.BOT.send_message.await_args.kwargs["reply_markup"]
        data = markup.inline_keyboard[0][0].callback_data
        self.assertLessEqual(len(data.encode()), 64)
        query = mock.AsyncMock()
        query.data = data
        query.from_user.id = 1
        update = mock.MagicMock(callback_query=query)
        update.effective_chat.id = 1
        context = mock.MagicMock()
        context.bot = mock.AsyncMock()
        create = mock.Mock(return_value={"title": "Brot"})
        with mock.patch.object(mahlino, "create_recipe_sync", create):
            asyncio.run(self.main.on_button(update, context))
        sent = json.dumps(create.call_args.args[0], ensure_ascii=False)
        self.assertIn("Mehl", sent)
        self.assertIn("gespeichert", context.bot.send_message.await_args.args[1])
        self.assertIsNone(recipes.take_unannounced(1))
        self.assertEqual(proposal["status"], "open")


if __name__ == "__main__":
    unittest.main()

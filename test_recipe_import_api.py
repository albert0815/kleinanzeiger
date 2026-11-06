"""Tests for the recipe import endpoint. Page and model are replaced."""

import json
import os
import unittest
import urllib.error
import urllib.request
from types import SimpleNamespace
from unittest import mock

import recipe_import_api as api
import recipes

PAGE = {
    "source_url": "https://rezept.example/brot",
    "structured_recipe": {"name": "Brot"},
    "page_text": None,
    "image_url": "https://rezept.example/brot.jpg",
}
FOUND = api.ImportedRecipe(
    is_recipe=True,
    title="Brot",
    servings=2,
    prep_minutes=10,
    instructions="1. Kneten\n\n2. Backen\nhttps://werbung.example",
    ingredients=[api.Ingredient(text="Mehl", quantity=500, unit="g")],
)


def fake_agent(found):
    agent = mock.Mock(return_value=SimpleNamespace(structured_output=found))
    return mock.patch.object(api, "_make_agent", return_value=agent)


class ImportTest(unittest.TestCase):
    def test_recipe_gets_mahlino_shape_and_page_link(self):
        with mock.patch.object(recipes, "read_recipe_page", return_value=PAGE), fake_agent(FOUND):
            result = api.import_recipe("https://rezept.example/brot")
        self.assertEqual(result["title"], "Brot")
        self.assertEqual(result["instructions"], "Kneten\nBacken")
        self.assertEqual(result["source_url"], "https://rezept.example/brot")
        self.assertEqual(result["image_url"], "https://rezept.example/brot.jpg")
        self.assertEqual(result["ingredients"][0], {"text": "Mehl", "quantity": 500.0, "unit": "g", "note": None})

    def test_page_without_recipe(self):
        with mock.patch.object(recipes, "read_recipe_page", return_value=PAGE), fake_agent(
            api.ImportedRecipe(is_recipe=False)
        ), self.assertRaises(recipes.RecipeError):
            api.import_recipe("https://rezept.example/brot")

    def test_prompt_contains_skill_rules(self):
        self.assertIn("instructions:", api.SYSTEM_PROMPT)


class ServerTest(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"MAHLINO_API_KEY": "geheim"})
        env.start()
        self.addCleanup(env.stop)
        port = mock.patch.object(api, "PORT", 0)
        port.start()
        self.addCleanup(port.stop)
        self.server = api.start_server()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/recipes/import"

    def post(self, body, key="geheim"):
        request = urllib.request.Request(
            self.url, data=json.dumps(body).encode(), headers={"X-API-Key": key}
        )
        try:
            with urllib.request.urlopen(request) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_wrong_key(self):
        self.assertEqual(self.post({"url": "https://x.example"}, key="falsch")[0], 401)

    def test_bad_body(self):
        self.assertEqual(self.post({"link": "x"})[0], 400)

    def test_error_is_reported_in_words(self):
        with mock.patch.object(api, "import_recipe", side_effect=recipes.RecipeError("Kaputt.")):
            self.assertEqual(self.post({"url": "x"}), (422, {"detail": "Kaputt."}))

    def test_success(self):
        with mock.patch.object(api, "import_recipe", return_value={"title": "Brot"}):
            self.assertEqual(self.post({"url": "https://x.example"}), (200, {"title": "Brot"}))

    def test_off_without_key(self):
        with mock.patch.dict(os.environ, {"MAHLINO_API_KEY": ""}):
            self.assertIsNone(api.start_server())


if __name__ == "__main__":
    unittest.main()

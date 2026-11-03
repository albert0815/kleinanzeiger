"""Tests for the lunch proposal flow, with Mahlino replaced by a fake."""

import asyncio
import datetime
import os
import tempfile
import unittest
from unittest import mock

import lunch_plan
import mahlino

MONDAY = datetime.date(2026, 11, 2)
TUESDAY = MONDAY + datetime.timedelta(days=1)
WEDNESDAY = MONDAY + datetime.timedelta(days=2)
RECIPES = [
    {"id": "suppe", "name": "Suppe", "lastPlanDate": "2026-01-01", "minutes": 20},
    {"id": "pasta", "name": "Pasta", "lastPlanDate": "2026-10-30", "minutes": 15},
]


def day(date, *names):
    return {"date": date.isoformat(), "recipes": [{"id": n, "name": n} for n in names]}


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"KLEINANZEIGEN_BASE_DIR": tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def validate(self, items, plan=()):
        return lunch_plan.validate_days(items, MONDAY, RECIPES, list(plan))


class ValidationTest(Base):
    def test_valid(self):
        result = self.validate([{"date": "2026-11-04", "recipe_id": "suppe"}])
        self.assertEqual(result[0]["name"], "Suppe")

    def test_rejected(self):
        cases = [
            ({"date": "2026-11-01", "recipe_id": "suppe"}, "Vergangenheit"),
            ({"date": str(TUESDAY), "recipe_id": "suppe"}, "Dienstag"),
            ({"date": "2026-11-04", "recipe_id": "gibtsnicht"}, "Unbekannt"),
            ({"date": "2026-11-04"}, "fehlt"),
            ({"date": "x", "recipe_id": "suppe"}, "Ungültig"),
        ]
        for item, expected in cases:
            self.assertIn(expected, self.validate([item]))

    def test_planned_day_needs_replace(self):
        plan = [day(WEDNESDAY, "Pasta")]
        item = {"date": str(WEDNESDAY), "recipe_id": "suppe"}
        self.assertIn("schon geplant", self.validate([item], plan))
        result = self.validate([{**item, "replace": True}], plan)
        self.assertEqual(result[0]["previous"], ["Pasta"])


class ShouldProposeTest(Base):
    def test_rules(self):
        self.assertFalse(lunch_plan.should_propose([], TUESDAY))
        self.assertFalse(lunch_plan.should_propose([day(WEDNESDAY, "Pasta")], WEDNESDAY))
        self.assertTrue(lunch_plan.should_propose([], WEDNESDAY))

    def test_only_once_a_week(self):
        lunch_plan.store_proposal([])
        now = datetime.datetime.now(datetime.UTC)
        self.assertFalse(lunch_plan.should_propose([], WEDNESDAY, now))
        later = now + datetime.timedelta(days=7)
        self.assertTrue(lunch_plan.should_propose([], WEDNESDAY, later))


class ApplyTest(Base):
    def days(self, **extra):
        return lunch_plan.validate_days(
            [{"date": str(WEDNESDAY), "recipe_id": "suppe", **extra}], MONDAY, RECIPES, []
        )

    def patched(self, entries):
        self.saved, self.deleted = [], []
        return (
            mock.patch.object(mahlino, "fetch_entry_ids_sync", lambda d: list(entries)),
            mock.patch.object(mahlino, "save_lunch_entry_sync", lambda d, s: self.saved.append((d, s))),
            mock.patch.object(mahlino, "delete_entry_sync", self.deleted.append),
        )

    def test_second_confirmation_does_not_save_twice(self):
        proposal = lunch_plan.store_proposal(self.days())
        a, b, c = self.patched([])
        with a, b, c:
            self.assertEqual(len(lunch_plan.apply_proposal(proposal["id"], MONDAY)["saved"]), 1)
            self.assertIsNone(lunch_plan.apply_proposal(proposal["id"], MONDAY))
        self.assertEqual(self.saved, [(str(WEDNESDAY), "suppe")])

    def test_day_filled_meanwhile_is_skipped(self):
        proposal = lunch_plan.store_proposal(self.days())
        a, b, c = self.patched([7])
        with a, b, c:
            result = lunch_plan.apply_proposal(proposal["id"], MONDAY)
        self.assertEqual(len(result["skipped"]), 1)
        self.assertEqual((self.saved, self.deleted), ([], []))

    def test_replace_removes_old_entry(self):
        proposal = lunch_plan.store_proposal(self.days(replace=True))
        a, b, c = self.patched([7])
        with a, b, c:
            lunch_plan.apply_proposal(proposal["id"], MONDAY)
        self.assertEqual(self.deleted, [7])
        self.assertEqual(len(self.saved), 1)

    def test_failure_keeps_proposal_open_for_retry(self):
        proposal = lunch_plan.store_proposal(self.days())
        with (
            mock.patch.object(mahlino, "fetch_entry_ids_sync", side_effect=OSError("weg")),
        ):
            with self.assertRaises(OSError):
                lunch_plan.apply_proposal(proposal["id"], MONDAY)
        self.assertEqual(lunch_plan.load_proposal()["status"], "open")

    def test_rejected_proposal_cannot_be_saved(self):
        proposal = lunch_plan.store_proposal(self.days())
        self.assertTrue(lunch_plan.reject_proposal(proposal["id"]))
        self.assertIsNone(lunch_plan.apply_proposal(proposal["id"], MONDAY))

    def test_old_proposal_id_cannot_be_saved(self):
        old = lunch_plan.store_proposal(self.days())
        lunch_plan.store_proposal(self.days())
        self.assertIsNone(lunch_plan.apply_proposal(old["id"], MONDAY))


class PlanningDataTest(Base):
    def test_filters_recent_and_marks_tuesday(self):
        many = RECIPES + [
            {"id": f"r{i}", "name": f"R{i}", "lastPlanDate": None, "minutes": None}
            for i in range(20)
        ]
        with (
            mock.patch.object(mahlino, "fetch_plan_sync", return_value=[day(MONDAY, "Reste")]),
            mock.patch.object(mahlino, "fetch_recipes_sync", return_value=many),
        ):
            data = lunch_plan.planning_data(MONDAY, WEDNESDAY, MONDAY)
        self.assertEqual(data["free_days"], [str(WEDNESDAY)])
        self.assertEqual(data["no_cooking_days"], [str(TUESDAY)])
        ids = [c["id"] for c in data["candidates"]]
        self.assertNotIn("pasta", ids)
        self.assertIn("suppe", ids)


class ToolAndBotTest(Base):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"ALLOWED_USERS": "1,2"})
        patcher.start()
        self.addCleanup(patcher.stop)
        import main
        import tools

        self.main, self.tools = main, tools
        main.BOT = mock.AsyncMock()
        import agent_registry

        self.tool_names = [t.tool_name for t in agent_registry._TOOLS]

    def test_agent_has_no_way_to_save(self):
        self.assertNotIn("save_lunch_plan", self.tool_names)
        self.assertIn("propose_lunch_plan", self.tool_names)

    def test_proposal_reaches_both_users_once_with_buttons(self):
        future = datetime.date.today() + datetime.timedelta(days=30)
        while future.weekday() == 1:
            future += datetime.timedelta(days=1)
        with (
            mock.patch.object(mahlino, "fetch_recipes_sync", return_value=RECIPES),
            mock.patch.object(mahlino, "fetch_plan_sync", return_value=[]),
        ):
            answer = self.tools.propose_lunch_plan(days=[{"date": str(future), "recipe_id": "suppe"}])
        self.assertIn("angelegt", answer)
        asyncio.run(self.main.announce_lunch_proposal())
        asyncio.run(self.main.announce_lunch_proposal())
        calls = self.main.BOT.send_message.await_args_list
        self.assertEqual({c.args[0] for c in calls}, {1, 2})
        self.assertEqual(len(calls), 2)
        button = calls[0].kwargs["reply_markup"].inline_keyboard[0][0]
        self.assertLessEqual(len(button.callback_data.encode()), 64)

    def test_accept_button_saves_without_agent(self):
        proposal = lunch_plan.store_proposal(
            lunch_plan.validate_days(
                [{"date": "2099-01-07", "recipe_id": "suppe"}],
                datetime.date(2099, 1, 1), RECIPES, [],
            )
        )
        query = mock.AsyncMock()
        query.data = f"plan_accept:{proposal['id']}"
        query.from_user.id = 1
        update = mock.MagicMock(callback_query=query)
        update.effective_chat.id = 1
        context = mock.MagicMock()
        context.bot = mock.AsyncMock()
        saved = []
        with (
            mock.patch.object(mahlino, "fetch_entry_ids_sync", return_value=[]),
            mock.patch.object(mahlino, "save_lunch_entry_sync", lambda d, s: saved.append((d, s))),
            mock.patch.object(self.main, "get_agent", side_effect=AssertionError("no agent")),
        ):
            asyncio.run(self.main.on_button(update, context))
        self.assertEqual(saved, [("2099-01-07", "suppe")])

    def test_week_shows_tuesday_and_notes(self):
        plan = [day(MONDAY, "Reste"), day(TUESDAY), day(WEDNESDAY)]
        text = self.main.format_week(plan)
        self.assertIn("Mo 02.11.: Reste", text)
        self.assertIn("Di 03.11.: Mama kocht", text)
        self.assertIn("Mi 04.11.: –", text)


if __name__ == "__main__":
    unittest.main()

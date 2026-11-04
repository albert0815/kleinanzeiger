"""Tests for category fields, publish errors and follow-up questions."""

import asyncio
import datetime
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

import ad_store
import bot_runner
import category_fields
from background_worker import BackgroundWorker
from queue_manager import QueueManager

CHAT = 42
BIKE = "Auto, Rad & Boot > Fahrräder & Zubehör > Zubehör"


def option(label, value):
    return {"label": label, "value": value}


FIELDS = [
    {"key": "fahrraeder.art", "attribute": "art_s", "label": "Art", "required": True, "preset": "zubehoer",
     "options": [option("Herren", "herren"), option("Zubehör", "zubehoer")]},
    {"key": "fahrraeder.type", "attribute": "type_s", "label": "Typ", "required": True, "preset": "",
     "options": [option("Cityräder", "city"), option("Andere Fahrräder", "sonstiges")]},
    {"key": "fahrraeder.color", "attribute": "color_s", "label": "Farbe", "required": False, "preset": "",
     "options": [option("Blau", "blau"), option("Schwarz", "schwarz")]},
    {"key": "fahrraeder.condition", "attribute": "condition_s", "label": "Zustand", "required": False,
     "preset": "", "options": []},
]
# What the patched bot logs (colours included).
REJECTED = (
    "\x1b[31m[ERROR]\x1b[0m \x1b[31mForm rejected for [\x1b[35mKindersitz\x1b[39m\x1b[31m]: Ad form validation failed: "
    "Typ [fahrraeder.type]: Bitte gib einen Wert ein. Correct the ad configuration before retrying.\x1b[0m\n"
)
INVALID = (
    "[ERROR] All 3 attempts failed for [Kindersitz]: Option 'Kindersitz' not found in button combobox "
    "'fahrraeder.type': Option not found (observed: ['Bitte wählen', 'Cityräder', 'Andere Fahrräder']). Skipping ad.\n"
)
UNKNOWN = "[ERROR] All 3 attempts failed for [Kindersitz]: Failed to set attribute 'farbe_s'. Skipping ad.\n"


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        (self.base / str(CHAT)).mkdir()
        categories = self.base / "categories.yaml"
        categories.write_text(f'"{BIKE}": 210/217/zubehoer\n', encoding="utf-8")
        env = mock.patch.dict(os.environ, {"KLEINANZEIGEN_BASE_DIR": tmp.name, "KLEINANZEIGEN_CATEGORIES_FILE": str(categories)})
        env.start()
        self.addCleanup(env.stop)
        ad_store._categories = None
        self.addCleanup(setattr, ad_store, "_categories", None)


class ValidateTest(Base):
    def test_label_or_value_maps_to_stored_value(self):
        chosen, missing = category_fields.validate(FIELDS, {"typ": "andere fahrräder", "Farbe": "blau", "Art": "Herren"})
        self.assertEqual(chosen, {"type_s": "sonstiges", "color_s": "blau"})
        self.assertEqual(missing, [])

    def test_unknown_value_leaves_required_missing(self):
        chosen, missing = category_fields.validate(FIELDS, {"Typ": "Kindersitz"})
        self.assertEqual(chosen, {})
        self.assertEqual([f["label"] for f in missing], ["Typ"])

    def test_agent_sees_only_open_fields(self):
        self.assertEqual([f["field"] for f in category_fields.describe_for_agent(FIELDS)], ["Typ", "Farbe"])

    def test_cache_and_expiry(self):
        category_fields.store(BIKE, FIELDS)
        self.assertEqual(category_fields.cached(BIKE), FIELDS)
        later = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=31)
        self.assertIsNone(category_fields.cached(BIKE, later))
        category_fields.invalidate(BIKE)
        self.assertIsNone(category_fields.cached(BIKE))


class ParseTest(unittest.TestCase):
    def test_missing_field_with_key(self):
        self.assertEqual(
            bot_runner.field_problems(REJECTED),
            [{"kind": "missing", "key": "fahrraeder.type", "label": "Typ", "message": "Bitte gib einen Wert ein"}],
        )

    def test_invalid_option_lists_choices(self):
        problem = bot_runner.field_problems(INVALID)[0]
        self.assertEqual((problem["kind"], problem["key"], problem["value"]), ("invalid", "fahrraeder.type", "Kindersitz"))
        self.assertEqual(problem["options"], ["Cityräder", "Andere Fahrräder"])

    def test_unknown_attribute(self):
        self.assertEqual(bot_runner.field_problems(UNKNOWN), [{"kind": "unknown", "attribute": "farbe_s"}])

    def test_other_errors_are_no_field_problems(self):
        self.assertEqual(bot_runner.field_problems("Failed to start browser"), [])

    def test_fields_command_output(self):
        output = 'noise\n\x1b[0mCATEGORY_FIELDS_JSON {"category": "x", "fields": [{"key": "a.b"}]}\n'
        with mock.patch.object(bot_runner, "run_bot", mock.AsyncMock(return_value=bot_runner.BotResult(0, output))) as run:
            self.assertEqual(asyncio.run(bot_runner.category_fields(CHAT, "210/217/zubehoer")), [{"key": "a.b"}])
        self.assertEqual(run.await_args.args[1:], ("fields", "--category=210/217/zubehoer"))

    def test_fields_command_failure(self):
        with mock.patch.object(bot_runner, "run_bot", mock.AsyncMock(return_value=bot_runner.BotResult(1, "kaputt"))):
            with self.assertRaises(bot_runner.BotFailure):
                asyncio.run(bot_runner.category_fields(CHAT, "x"))


class WorkerTest(unittest.TestCase):
    def test_permanent_failure_goes_straight_to_backout_and_notifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = QueueManager(Path(tmp))
            queue.enqueue("j", CHAT, "publish_ad", {})
            notified = []

            async def handler(_data):
                return False, "Typ fehlt", True

            async def notify(job_id, chat_id, success, message):
                notified.append(message)

            async def run():
                worker = BackgroundWorker(queue, {"publish_ad": handler}, notify)
                await worker.start()
                for _ in range(50):
                    if notified:
                        break
                    await asyncio.sleep(0.05)
                await worker.stop()

            asyncio.run(run())
            self.assertEqual(notified, ["Typ fehlt"])
            self.assertIn("j", queue.backout)


class MainTest(Base):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"ALLOWED_USERS": str(CHAT)})
        patcher.start()
        self.addCleanup(patcher.stop)
        import main

        self.main = main
        main.BOT = mock.AsyncMock()
        for name, value in (("KLEINANZEIGEN_BOT_CMD", "kleinanzeigen"), ("ALLOWED_USER_IDS", {CHAT})):
            p = mock.patch.object(main, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.enqueue = mock.Mock()
        p = mock.patch.object(main, "QUEUE_MANAGER", mock.Mock(enqueue=self.enqueue))
        p.start()
        self.addCleanup(p.stop)

    def draft(self):
        d = self.main.Draft(photos=[b"x"], title="Kindersitz", category=BIKE, condition="Gut", description="Britax", price_eur=35)
        self.main.DRAFTS[CHAT] = d
        self.addCleanup(self.main.DRAFTS.pop, CHAT, None)
        return d

    def complete(self, d, answer, fields=None):
        fields_call = mock.AsyncMock(return_value=FIELDS) if fields is None else fields
        with (
            mock.patch.object(bot_runner, "category_fields", fields_call),
            mock.patch.object(self.main, "_call_agent", mock.AsyncMock(return_value=answer)),
        ):
            async def go():
                task = self.main.start_field_completion(CHAT, d)
                self.assertEqual(d.fields_state, "pending")
                await task

            asyncio.run(go())
        return fields_call

    def press(self, data):
        query = mock.AsyncMock()
        query.data = data
        query.from_user.id = CHAT
        update = mock.MagicMock(callback_query=query)
        update.effective_chat.id = CHAT
        context = mock.MagicMock()
        context.bot = mock.AsyncMock()
        asyncio.run(self.main.on_button(update, context))
        return context.bot.send_message

    def test_draft_gets_required_field_and_writes_stored_value(self):
        d = self.draft()
        call = self.complete(d, '{"title": "Kindersitz", "attributes": {"Typ": "Andere Fahrräder"}}')
        call.assert_awaited_once_with(CHAT, BIKE)
        self.assertEqual((d.fields_state, d.attributes, d.missing_fields), ("ready", {"type_s": "sonstiges"}, []))
        self.assertIn("Typ: Andere Fahrräder", self.main.render_draft(d))
        text = self.main.write_ad_files(d, CHAT).read_text(encoding="utf-8")
        self.assertIn('  type_s: "sonstiges"', text)
        self.assertIn("  condition_s: ok", text)
        self.assertNotIn("art_s", text)

    def test_second_draft_uses_cache(self):
        self.complete(self.draft(), "{}")
        self.complete(self.draft(), "{}").assert_not_awaited()

    def test_missing_required_blocks_publish_until_answered(self):
        d = self.draft()
        self.complete(d, '{"title": "Kindersitz", "attributes": {"Typ": "Kindersitz"}}')
        self.assertIn("Pflichtfeld fehlt", self.main.render_draft(d))
        sent = self.press("publish")
        self.enqueue.assert_not_called()
        self.assertIn("Typ", sent.await_args.args[1])
        self.main._apply_dict_to_draft(d, {"title": "Kindersitz", "attributes": {"Typ": "Cityräder"}})
        self.assertEqual(d.missing_fields, [])

    def test_reading_failure_does_not_block(self):
        d = self.draft()
        self.complete(d, "{}", fields=mock.AsyncMock(side_effect=bot_runner.BotFailure("login", "nicht angemeldet")))
        self.assertEqual((d.fields_state, d.missing_fields), ("failed", []))

    def write_ad(self, extra=""):
        folder = self.base / str(CHAT) / "ads" / "neu"
        folder.mkdir(parents=True)
        ad_file = folder / "ad_new.yaml"
        ad_file.write_text(f'active: true\ntitle: Kindersitz\ncategory: "{BIKE}"\nprice: 35\n{extra}', encoding="utf-8")
        return ad_file

    def publish(self, ad_file, output, fields=None):
        self.fields_call = fields or mock.AsyncMock(return_value=FIELDS)
        with (
            mock.patch.object(bot_runner, "publish_new", mock.AsyncMock(return_value=bot_runner.BotResult(1, output))),
            mock.patch.object(bot_runner, "category_fields", self.fields_call),
        ):
            return asyncio.run(self.main.handle_publish_job({"chat_id": CHAT, "ad_file": str(ad_file)}))

    def test_missing_field_asks_without_retry_then_answer_publishes(self):
        ad_file = self.write_ad()
        ok, message, permanent = self.publish(ad_file, REJECTED)
        self.assertEqual((ok, permanent), (False, True))
        self.fields_call.assert_awaited_once_with(CHAT, BIKE)
        self.assertIn("Typ", message)
        self.assertIn("Andere Fahrräder", message)  # options from the fresh read
        self.assertEqual(category_fields.cached(BIKE), FIELDS)
        with mock.patch.object(self.main, "_call_agent", mock.AsyncMock(return_value='{"answers": {"Typ": "Andere Fahrräder"}}')):
            self.assertTrue(asyncio.run(self.main.answer_publish_question(CHAT, "andere")))
        self.assertEqual(yaml.safe_load(ad_file.read_text())["special_attributes"], {"type_s": "Andere Fahrräder"})
        self.enqueue.assert_called_once()
        self.assertEqual(self.main._load_questions(), {})

    def test_failed_refresh_falls_back_to_cached_options_and_drops_cache(self):
        category_fields.store(BIKE, FIELDS)
        failing = mock.AsyncMock(side_effect=bot_runner.BotFailure("timeout", "zu langsam"))
        ok, message, permanent = self.publish(self.write_ad(), REJECTED, fields=failing)
        self.assertIn("Andere Fahrräder", message)
        self.assertIsNone(category_fields.cached(BIKE))

    def test_failed_refresh_without_cache_asks_without_options(self):
        failing = mock.AsyncMock(side_effect=bot_runner.BotFailure("timeout", "zu langsam"))
        ok, message, permanent = self.publish(self.write_ad(), REJECTED, fields=failing)
        self.assertIn("• Typ", message)
        self.assertNotIn("Möglich", message)
        self.assertTrue(permanent)

    def test_unknown_attribute_does_not_read_fields(self):
        self.publish(self.write_ad("special_attributes:\n  farbe_s: blau\n"), UNKNOWN)
        self.fields_call.assert_not_awaited()

    def test_invalid_option_asks_with_offered_choices(self):
        ad_file = self.write_ad("special_attributes:\n  type_s: Kindersitz\n")
        ok, message, permanent = self.publish(ad_file, INVALID)
        self.assertIn("«Kindersitz» gibt es nicht", message)
        self.assertIn("Cityräder", message)
        with mock.patch.object(self.main, "_call_agent", mock.AsyncMock(return_value='{"answers": {"Typ": "Kindersitz"}}')):
            self.assertTrue(asyncio.run(self.main.answer_publish_question(CHAT, "Kindersitz")))
        self.enqueue.assert_not_called()  # still not a valid option, asked again
        self.assertIn("Möglich", self.main.BOT.send_message.await_args.args[1])

    def test_unrelated_chat_message_is_not_taken_as_answer(self):
        self.publish(self.write_ad(), REJECTED)
        with mock.patch.object(self.main, "_call_agent", mock.AsyncMock(return_value='{"answers": {}}')):
            self.assertFalse(asyncio.run(self.main.answer_publish_question(CHAT, "Was gibt es heute?")))
        self.assertTrue(self.main._load_questions())

    def test_unknown_attribute_is_removed_and_published_again(self):
        ad_file = self.write_ad("special_attributes:\n  farbe_s: blau\n  condition_s: ok\n")
        ok, message, permanent = self.publish(ad_file, UNKNOWN)
        self.assertTrue(permanent)
        self.assertEqual(yaml.safe_load(ad_file.read_text())["special_attributes"], {"condition_s": "ok"})
        self.enqueue.assert_called_once()

    def test_other_failure_reports_reason(self):
        output = "[ERROR] Category resolution failed for [Kindersitz]: Category dialog has no unique match for '210'"
        ok, message, permanent = self.publish(self.write_ad(), output)
        self.assertIn("no unique match", message)
        self.assertTrue(permanent)


if __name__ == "__main__":
    unittest.main()

"""Tests for online sync, queue behaviour and the bot adapter. No network needed."""

import asyncio
import datetime
import os
import stat
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import ad_store
import bot_runner
from ad_store import OnlineAd
from queue_manager import JobStatus, QueueManager

CHAT = 42


def write_ad(base, name, *, ad_id=None, active=True, title="Titel"):
    folder = base / str(CHAT) / "ads" / name
    folder.mkdir(parents=True)
    path = folder / "ad.yaml"
    identifier = f"id: {ad_id}\n" if ad_id else ""
    path.write_text(
        f'active: {"true" if active else "false"}\n{identifier}title: "{title}"\nprice: 5\n',
        encoding="utf-8",
    )
    return path


class TempBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        patcher = mock.patch.dict(os.environ, {"KLEINANZEIGEN_BASE_DIR": tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        (self.base / str(CHAT)).mkdir()


class ParseListTest(unittest.TestCase):
    def test_parses_entries_with_ansi_colours(self):
        output = (
            "\x1b[30m[INFO]\x1b[0m Reached last page 1 of 1\n"
            "ID: 123\n  title: Paw Patrol\n  status: reserved\n  expires: 06.12.2026\n\n"
            "ID: 456\n  title: Buch\n  status: active\n  expires: -\n"
        )
        ads = bot_runner.parse_list_output(output)
        self.assertEqual([a.ad_id for a in ads], ["123", "456"])
        self.assertEqual(ads[0].state, "reserved")

    def test_empty_account_is_valid(self):
        self.assertEqual(bot_runner.parse_list_output("No online ads found."), [])

    def test_unreadable_output_is_a_failure_not_an_empty_list(self):
        with self.assertRaises(bot_runner.BotFailure):
            bot_runner.parse_list_output("Ausgabe in neuem Format")

    def test_incomplete_pagination_is_a_failure(self):
        output = "Pagination request failed on page 2\nID: 1\n  title: A\n  status: active\n  expires: -\n"
        with self.assertRaises(bot_runner.BotFailure):
            bot_runner.parse_list_output(output)


class ClassifyTest(unittest.TestCase):
    def test_kinds(self):
        classify = bot_runner.classify_failure
        self.assertEqual(classify("No HTML element found with ID 'username'"), "login")
        self.assertEqual(classify("Failed to start browser. x"), "browser")
        self.assertEqual(classify("TimeoutError: Page did not finish"), "timeout")
        self.assertEqual(classify("kaputt"), "other")


class SyncTest(TempBase):
    def status(self):
        return {a["title"]: a["status"] for a in ad_store.overview(CHAT)["ads"]}

    def test_status_derivation(self):
        write_ad(self.base, "a", ad_id=1, title="online")
        write_ad(self.base, "b", ad_id=2, title="weg")
        write_ad(self.base, "c", title="neu")
        write_ad(self.base, "d", ad_id=4, active=False, title="aus")
        write_ad(self.base, "e", ad_id=5, title="res")
        self.assertEqual(self.status()["weg"], "unknown")
        ad_store.apply_sync(
            CHAT,
            [
                OnlineAd("1", "online", "active", "-"),
                OnlineAd("5", "res", "reserved", "-"),
                OnlineAd("9", "fremd", "active", "-"),
            ],
        )
        self.assertEqual(
            self.status(),
            {"online": "online", "weg": "missing", "neu": "draft", "aus": "inactive", "res": "reserved"},
        )
        self.assertEqual([a["ad_id"] for a in ad_store.overview(CHAT)["online_only"]], ["9"])

    def test_differences_are_reported_only_once(self):
        write_ad(self.base, "a", ad_id=1, title="A")
        first = ad_store.apply_sync(CHAT, [])
        self.assertTrue(first["first_sync"])
        self.assertEqual(len(first["missing"]), 1)
        second = ad_store.apply_sync(CHAT, [])
        self.assertFalse(second["first_sync"])
        self.assertEqual(second["missing"], [])
        ad_store.apply_sync(CHAT, [OnlineAd("1", "A", "active", "-")])
        third = ad_store.apply_sync(CHAT, [])
        self.assertEqual(len(third["missing"]), 1)

    def test_sold_ad_is_not_reported_missing(self):
        path = write_ad(self.base, "a", ad_id=1, title="A")
        ad_store.record_sold(CHAT, ad_store.load_ad_file(CHAT, path))
        self.assertEqual(ad_store.apply_sync(CHAT, [])["missing"], [])
        self.assertEqual(self.status()["A"], "sold")

    def test_expiring_soon(self):
        write_ad(self.base, "a", ad_id=1, title="A")
        now = datetime.datetime(2026, 12, 1, tzinfo=datetime.UTC)
        report = ad_store.apply_sync(CHAT, [OnlineAd("1", "A", "active", "06.12.2026")], now)
        self.assertEqual(len(report["expiring"]), 1)
        report = ad_store.apply_sync(CHAT, [OnlineAd("1", "A", "active", "06.02.2027")], now)
        self.assertEqual(report["expiring"], [])

    def test_health_change_detection(self):
        self.assertFalse(ad_store.update_health(CHAT, True, None, None))
        self.assertTrue(ad_store.update_health(CHAT, False, "login", "x"))
        self.assertFalse(ad_store.update_health(CHAT, False, "login", "x"))
        self.assertTrue(ad_store.update_health(CHAT, True, None, None))

    def test_key_is_short_enough_for_telegram_buttons(self):
        write_ad(self.base, "a" * 60, title="A")
        key = ad_store.list_local_ads(CHAT)[0].key
        self.assertLessEqual(len(f"deact:{key}".encode()), 64)
        self.assertEqual(ad_store.find_ad(CHAT, key).title, "A")

    def test_set_active_keeps_rest_of_file(self):
        path = write_ad(self.base, "a", ad_id=1)
        ad_store.set_active(path, False)
        ad = ad_store.load_ad_file(CHAT, path)
        self.assertFalse(ad.active)
        self.assertEqual(ad.ad_id, "1")


class CategoryTest(TempBase):
    def setUp(self):
        super().setUp()
        path = self.base / "categories.yaml"
        path.write_text(
            'Familie, Kind & Baby > Baby- & Kinderschuhe: 17/19/sonstiges\n'
            'Elektronik > Notebooks: 161/278\n',
            encoding="utf-8",
        )
        patcher = mock.patch.dict(os.environ, {"KLEINANZEIGEN_CATEGORIES_FILE": str(path)})
        patcher.start()
        self.addCleanup(patcher.stop)
        ad_store._categories = None
        self.addCleanup(setattr, ad_store, "_categories", None)

    def test_resolve(self):
        resolve = ad_store.resolve_category
        self.assertEqual(resolve("elektronik > notebooks"), "Elektronik > Notebooks")
        self.assertEqual(resolve("161/278"), "161/278")
        self.assertEqual(
            resolve("Familie, Kind & Baby > Kinderschuhe"),
            "Familie, Kind & Baby > Baby- & Kinderschuhe",
        )
        self.assertIsNone(resolve("Auto > Reifen"))

    def test_search(self):
        self.assertEqual(
            ad_store.search_categories("kinderschuhe"),
            ["Familie, Kind & Baby > Baby- & Kinderschuhe"],
        )


class QueueTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def test_interrupted_job_resumes_after_restart(self):
        queue = QueueManager(self.dir)
        queue.enqueue("j", CHAT, "publish_ad", {})
        queue.mark_processing("j")
        self.assertEqual(QueueManager(self.dir).get_next_job().job_id, "j")

    def test_failed_job_waits_before_retry(self):
        queue = QueueManager(self.dir)
        queue.enqueue("j", CHAT, "publish_ad", {})
        queue.mark_failed("j", "boom")
        self.assertIsNone(queue.get_next_job())
        self.assertEqual(queue.pending["j"].status, JobStatus.PENDING.value)
        queue.pending["j"].next_attempt_at = "2000-01-01T00:00:00"
        self.assertEqual(queue.get_next_job().job_id, "j")


class RunBotTest(TempBase):
    def fake_bot(self, body):
        script = self.base / "fake-bot"
        script.write_text("#!/bin/sh\n" + textwrap.dedent(body))
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        (self.base / str(CHAT) / "config.yaml").write_text("{}")
        patcher = mock.patch.dict(os.environ, {"KLEINANZEIGEN_BOT_CMD": str(script)})
        patcher.start()
        self.addCleanup(patcher.stop)
        bot_runner._LOCKS.clear()

    def test_delete_uses_numeric_id_selector(self):
        self.fake_bot('echo "$@"\n')
        result = asyncio.run(bot_runner.delete_online(CHAT, "123"))
        self.assertIn("delete --ads=123", result.output)

    def test_browser_start_race_is_retried_and_locks_cleared(self):
        counter = self.base / "count"
        self.fake_bot(
            f'echo x >> {counter}\n[ "$(wc -l < {counter})" -ge 2 ] && exit 0\n'
            'echo "Failed to start browser"; exit 1\n'
        )
        profile = self.base / str(CHAT) / ".temp" / "browser-profile"
        profile.mkdir(parents=True)
        (profile / "SingletonLock").symlink_to("old-host-1")
        with mock.patch("asyncio.sleep", new=mock.AsyncMock()):
            result = asyncio.run(bot_runner.run_bot(CHAT, "list"))
        self.assertEqual(result.returncode, 0)
        self.assertFalse((profile / "SingletonLock").is_symlink())

    def test_hanging_bot_is_killed(self):
        self.fake_bot("sleep 30\n")
        with self.assertRaises(bot_runner.BotFailure) as caught:
            asyncio.run(bot_runner.run_bot(CHAT, "list", timeout=0.2))
        self.assertEqual(caught.exception.kind, "timeout")

    def test_list_failure_is_classified(self):
        self.fake_bot("echo \"No HTML element found with ID 'username'\"; exit 1\n")
        with self.assertRaises(bot_runner.BotFailure) as caught:
            asyncio.run(bot_runner.list_online(CHAT))
        self.assertEqual(caught.exception.kind, "login")


class MainFlowTest(TempBase):
    """Job handlers in main.py with the bot and Telegram replaced."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"ALLOWED_USERS": str(CHAT)})
        patcher.start()
        self.addCleanup(patcher.stop)
        import main

        self.main = main
        main.BOT = mock.AsyncMock()

    def test_publish_touches_only_its_own_ad_and_requires_id(self):
        target = write_ad(self.base, "ziel")
        other = write_ad(self.base, "anderes", title="Anderes")
        seen = {}

        async def fake_publish(chat_id):
            seen["other_active"] = ad_store.load_ad_file(chat_id, other).active
            text = target.read_text()
            target.write_text(text + "id: 777\n")
            return bot_runner.BotResult(0, "")

        with (
            mock.patch.object(bot_runner, "publish_new", fake_publish),
            mock.patch.object(bot_runner, "list_online", mock.AsyncMock(return_value=[])),
        ):
            ok, message = asyncio.run(
                self.main.handle_publish_job({"chat_id": CHAT, "ad_file": str(target)})
            )
        self.assertTrue(ok)
        self.assertIn("777", message)
        self.assertFalse(seen["other_active"])
        self.assertTrue(ad_store.load_ad_file(CHAT, other).active)

    def test_publish_with_rc_zero_but_no_id_fails(self):
        target = write_ad(self.base, "ziel")
        result = bot_runner.BotResult(0, "DONE: (Re-)published 0 ads (1 failed after retries)")
        with mock.patch.object(bot_runner, "publish_new", mock.AsyncMock(return_value=result)):
            ok, _ = asyncio.run(
                self.main.handle_publish_job({"chat_id": CHAT, "ad_file": str(target)})
            )
        self.assertFalse(ok)

    def test_retry_after_success_does_not_publish_twice(self):
        target = write_ad(self.base, "ziel", ad_id=5)
        publish = mock.AsyncMock()
        with mock.patch.object(bot_runner, "publish_new", publish):
            ok, _ = asyncio.run(
                self.main.handle_publish_job({"chat_id": CHAT, "ad_file": str(target)})
            )
        self.assertTrue(ok)
        publish.assert_not_called()

    def test_sold_deletes_online_verifies_and_records(self):
        path = write_ad(self.base, "a", ad_id=1, title="A")
        online = OnlineAd("1", "A", "active", "-")
        delete = mock.AsyncMock(return_value=bot_runner.BotResult(0, ""))
        with (
            mock.patch.object(bot_runner, "delete_online", delete),
            mock.patch.object(bot_runner, "list_online", mock.AsyncMock(side_effect=[[online], []])),
        ):
            ok, _ = asyncio.run(
                self.main.handle_delete_job({"chat_id": CHAT, "ad_file": str(path), "sold": True})
            )
        self.assertTrue(ok)
        delete.assert_awaited_once_with(CHAT, "1")
        self.assertFalse(ad_store.load_ad_file(CHAT, path).active)
        self.assertEqual(ad_store.overview(CHAT)["ads"][0]["status"], "sold")

    def test_delete_fails_if_still_online(self):
        path = write_ad(self.base, "a", ad_id=1)
        online = OnlineAd("1", "A", "active", "-")
        with (
            mock.patch.object(bot_runner, "delete_online", mock.AsyncMock(return_value=bot_runner.BotResult(0, ""))),
            mock.patch.object(bot_runner, "list_online", mock.AsyncMock(return_value=[online])),
        ):
            ok, _ = asyncio.run(
                self.main.handle_delete_job({"chat_id": CHAT, "ad_file": str(path)})
            )
        self.assertFalse(ok)
        self.assertTrue(ad_store.load_ad_file(CHAT, path).active)

    def test_failed_sync_is_reported_once_and_recovery_once(self):
        failure = bot_runner.BotFailure("login", "x")
        with mock.patch.object(bot_runner, "list_online", mock.AsyncMock(side_effect=failure)):
            asyncio.run(self.main.sync_account(CHAT))
            asyncio.run(self.main.sync_account(CHAT))
        self.assertEqual(self.main.BOT.send_message.await_count, 1)
        self.main.BOT.send_message.reset_mock()
        with mock.patch.object(bot_runner, "list_online", mock.AsyncMock(return_value=[])):
            asyncio.run(self.main.sync_account(CHAT))
        texts = [c.args[1] for c in self.main.BOT.send_message.await_args_list]
        self.assertTrue(any("klappt wieder" in t for t in texts))

    def test_written_ad_is_valid_and_rejects_unknown_category(self):
        draft = self.main.Draft(
            photos=[b"x"], title="T", category="161/278", condition="Sehr gut", description="D", price_eur=3
        )
        ad_file = self.main.write_ad_files(draft, CHAT)
        text = ad_file.read_text()
        self.assertIn("condition_s: like_new", text)
        self.main._strip_condition(ad_file)
        self.assertNotIn("special_attributes", ad_file.read_text())
        draft.category = "Gibt es nicht"
        with mock.patch.object(ad_store, "_categories", ["Elektronik > Notebooks"]):
            with self.assertRaises(ValueError):
                self.main.write_ad_files(draft, CHAT)

    def test_ad_list_renders_within_telegram_limits(self):
        for i in range(120):
            write_ad(self.base, f"ad{i:03}", ad_id=i + 1, title="Sehr langer Titel " * 5)
        ad_store.apply_sync(CHAT, [])
        text, markup = self.main._render_ad_list(CHAT)
        buttons = [b for row in markup.inline_keyboard for b in row]
        self.assertEqual(len(buttons), 100)
        self.assertTrue(all(len(b.callback_data.encode()) <= 64 for b in buttons))
        self.assertIn("fehlt online", text)


if __name__ == "__main__":
    unittest.main()

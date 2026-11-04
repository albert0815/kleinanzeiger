"""Tests for search watches. Kleinanzeigen is replaced by fixed pages."""

import asyncio
import os
import tempfile
import unittest
from unittest import mock

import httpx

import search_watch

CHAT = 4711
REAL_CLIENT = httpx.Client


def article(
    ad_id, title="Lego Technic 42056", price="150 € VB", place="85221 Dachau", km="(16 km)",
    date="Heute, 12:55",
):
    distance = f'<span class="x">{km}</span>' if km else ""
    when = f"<div><svg><path d='M2'></path></svg><span>{date}</span></div>" if date else ""
    return f"""<li><article class="flex" data-adid="{ad_id}" data-href="/s-anzeige/x/{ad_id}-23-1">
<div><script type="application/ld+json">{{"title":"ignored"}}</script>
<a href="/s-anzeige/x/{ad_id}-23-1"><img src="a.jpg"><div>9</div></a></div>
<div><div><div><svg viewBox="0 0 24 24"><path d="M1"></path></svg><span>{place}</span>{distance}</div>
{when}</div>
<div><h3 class="t"><a href="/s-anzeige/x/{ad_id}-23-1">{title}</a></h3>
<p class="d">Beschreibung …</p><div class="p"><p class="p">{price}</p></div></div>
<div><p><span data-dhl-promotion>Versand möglich</span></p></div></div></article></li>"""


def page(*ids, alternatives=(), top=()):
    results = (
        '<ul id="srchrslt-adtable">'
        + "".join(article(i, date=None) for i in top)
        + "".join(article(i) for i in ids)
        + "</ul>"
        if ids or top
        else '<section id="saved-search-empty-result"><p>Keine Ergebnisse</p></section>'
    )
    alt = (
        f'<ul id="srchrslt-adtable-altads">{"".join(article(i) for i in alternatives)}</ul>'
        if alternatives
        else ""
    )
    return f'<html><body id="srchrslt"><div id="srchrslt-results">{results}{alt}</div></body></html>'


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"KLEINANZEIGEN_BASE_DIR": tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        pause = mock.patch.object(search_watch, "PAGE_PAUSE_SECONDS", (0, 0))
        pause.start()
        self.addCleanup(pause.stop)

    def serve(self, *pages, locations=None):
        """Answer search requests with the given pages, one per request."""
        remaining = list(pages)
        self.requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if request.url.path.endswith("s-ort-empfehlungen.json"):
                return httpx.Response(200, json=locations or {"_0": "Deutschland"})
            item = remaining.pop(0)
            if isinstance(item, int):
                return httpx.Response(item, text="error")
            return httpx.Response(200, text=item)

        transport = httpx.MockTransport(handler)
        patcher = mock.patch.object(
            search_watch.httpx,
            "Client",
            lambda **kw: REAL_CLIENT(transport=transport, **kw),
        )
        patcher.start()
        self.addCleanup(patcher.stop)


class ParseTest(unittest.TestCase):
    def test_reads_fields(self):
        [hit] = search_watch.parse_results(page("111"))
        self.assertEqual(hit.id, "111")
        self.assertEqual(hit.title, "Lego Technic 42056")
        self.assertEqual(hit.price, "150 € VB")
        self.assertEqual(hit.location, "85221 Dachau (16 km)")
        self.assertEqual(hit.date, "Heute, 12:55")
        self.assertEqual(hit.url, "https://www.kleinanzeigen.de/s-anzeige/x/111-23-1")

    def test_without_distance_and_with_entities(self):
        html = f'<ul id="srchrslt-adtable">{article("5", title="Lego &amp; Duplo", price=" Zu verschenken", km="")}</ul>'
        [hit] = search_watch.parse_results(html)
        self.assertEqual(hit.title, "Lego & Duplo")
        self.assertEqual(hit.price, "Zu verschenken")
        self.assertEqual(hit.location, "85221 Dachau")
        self.assertEqual(hit.date, "Heute, 12:55")

    def test_ignores_alternative_ads(self):
        self.assertEqual(search_watch.parse_results(page(alternatives=["9", "8"])), [])
        hits = search_watch.parse_results(page("1", alternatives=["9"]))
        self.assertEqual([h.id for h in hits], ["1"])

    def test_skips_top_ads_without_date(self):
        hits = search_watch.parse_results(page("3", "1", top=["1", "7"]))
        self.assertEqual([h.id for h in hits], ["3", "1"])
        html = f'<ul id="srchrslt-adtable">{article("4", date="02.11.2026")}{article("5", date="Gestern, 18:38")}</ul>'
        self.assertEqual([h.date for h in search_watch.parse_results(html)], ["02.11.2026", "Gestern, 18:38"])

    def test_unexpected_page_is_an_error(self):
        with self.assertRaises(search_watch.WatchError):
            search_watch.parse_results("<html>Bitte bestätige, dass du kein Roboter bist</html>")


class WatchTest(Base):
    def test_nationwide_watch_reports_only_new_hits(self):
        self.serve(page("2", "1"), page("3", "2", "1"), page("3", "2", "1"))
        watch, hits = search_watch.create_watch(CHAT, "  lego   technic ", max_price=50)
        self.assertEqual([h.id for h in hits], ["2", "1"])
        self.assertEqual(watch["query"], "lego technic")
        params = self.requests[0].url.params
        self.assertEqual(params["keywords"], "lego technic")
        self.assertEqual(params["sortingField"], "SORTING_DATE")
        self.assertEqual(params["maxPrice"], "50")
        self.assertNotIn("locationId", params)

        first = search_watch.check_watch(CHAT, watch["id"])
        self.assertEqual([h.id for h in first.new_hits], ["3"])
        second = search_watch.check_watch(CHAT, watch["id"])
        self.assertEqual(second.new_hits, [])

    def test_radius_watch_resolves_location(self):
        self.serve(page(), locations={"_0": "Deutschland", "_6443": "80331 Altstadt-Lehel"})
        watch, hits = search_watch.create_watch(CHAT, "rennrad", "80331", radius_km=25)
        self.assertEqual(hits, [])
        self.assertEqual(watch["location_id"], "6443")
        self.assertEqual(watch["radius_km"], 20)
        self.assertEqual(self.requests[0].url.params["query"], "80331")
        search = self.requests[1].url.params
        self.assertEqual((search["locationId"], search["radius"]), ("6443", "20"))
        self.assertEqual(
            search_watch.describe(watch), "«rennrad» · 20 km um 80331 Altstadt-Lehel"
        )

    def test_unknown_location(self):
        self.serve(locations={"_0": "Deutschland"})
        with self.assertRaises(search_watch.WatchError):
            search_watch.create_watch(CHAT, "rennrad", "Nirgendwo")
        self.assertEqual(search_watch.list_watches(CHAT), [])

    def test_rejects_bad_input(self):
        for args in (("",), ("x", None, None, -1), ("x", None, None, 50, 10)):
            with self.assertRaises(search_watch.WatchError):
                search_watch.create_watch(CHAT, *args)

    def test_ads_moving_up_are_not_reported_again(self):
        self.serve(page("3", "2", "1"), page("3", "1"), page("3", "1", "0"))
        watch, _ = search_watch.create_watch(CHAT, "x")
        self.assertEqual(search_watch.check_watch(CHAT, watch["id"]).new_hits, [])
        # "0" was never on the page before, "2" was deleted.
        self.assertEqual(
            [h.id for h in search_watch.check_watch(CHAT, watch["id"]).new_hits], ["0"]
        )

    def test_reads_further_pages_until_known_results(self):
        self.serve(page("1"), page("4", "3", top=["9"]), page("3", "2", "1", top=["9"]))
        watch, _ = search_watch.create_watch(CHAT, "x")
        result = search_watch.check_watch(CHAT, watch["id"])
        self.assertEqual([h.id for h in result.new_hits], ["4", "3", "2"])
        self.assertTrue(result.complete)
        self.assertEqual(self.requests[2].url.params["pageNum"], "2")
        self.assertNotIn("pageNum", self.requests[1].url.params)

    def test_stops_after_last_page_and_page_limit(self):
        # Past the last page the site shows it again.
        self.serve(page(), page("2", "1"), page("2", "1"))
        watch, _ = search_watch.create_watch(CHAT, "x")
        result = search_watch.check_watch(CHAT, watch["id"])
        self.assertEqual(([h.id for h in result.new_hits], result.complete), (["2", "1"], True))

        pages = [page(str(10 * p + 1), str(10 * p)) for p in range(search_watch.MAX_PAGES)]
        self.serve(page(), *pages)
        watch, _ = search_watch.create_watch(CHAT, "y")
        result = search_watch.check_watch(CHAT, watch["id"])
        self.assertEqual(len(result.new_hits), 2 * search_watch.MAX_PAGES)
        self.assertFalse(result.complete)

    def test_failures_are_reported_once_and_recovery_too(self):
        self.serve(page("1"), 503, 503, 503, page("2", "1"))
        watch, _ = search_watch.create_watch(CHAT, "x")
        notices = [search_watch.check_watch(CHAT, watch["id"]).notice for _ in range(3)]
        self.assertEqual(notices, [None, "failed", None])
        result = search_watch.check_watch(CHAT, watch["id"])
        self.assertEqual(result.notice, "recovered")
        self.assertEqual([h.id for h in result.new_hits], ["2"])

    def test_delete_and_watches_per_chat(self):
        self.serve(page("1"), page("1"))
        a, _ = search_watch.create_watch(CHAT, "a")
        b, _ = search_watch.create_watch(CHAT + 1, "b")
        self.assertEqual(search_watch.all_chat_ids(), [CHAT, CHAT + 1])
        self.assertIsNone(search_watch.delete_watch(CHAT, b["id"]))
        self.assertEqual(search_watch.delete_watch(CHAT, a["id"])["query"], "a")
        self.assertEqual(search_watch.list_watches(CHAT), [])
        self.assertIsNone(search_watch.check_watch(CHAT, a["id"]))

    def test_limit_per_chat(self):
        self.serve(*[page("1")] * search_watch.MAX_WATCHES_PER_CHAT)
        for i in range(search_watch.MAX_WATCHES_PER_CHAT):
            search_watch.create_watch(CHAT, f"q{i}")
        with self.assertRaises(search_watch.WatchError):
            search_watch.create_watch(CHAT, "zu viel")


GEROLSBACH = {"_0": "Deutschland", "_15559": "Gerolsbach - Bayern"}


class UpdateTest(Base):
    def test_nationwide_to_radius_and_back(self):
        self.serve(page("1"), page("5", "4"), page("9"), locations=GEROLSBACH)
        watch, _ = search_watch.create_watch(CHAT, "lego", max_price=50)

        changed, hits = search_watch.update_watch(CHAT, watch["id"], location="Gerolsbach")
        self.assertEqual([h.id for h in hits], ["5", "4"])
        self.assertEqual(
            search_watch.describe(changed), "«lego» bis 50 € · 30 km um Gerolsbach - Bayern"
        )
        [stored] = search_watch.list_watches(CHAT)
        self.assertEqual((stored["id"], stored["seen"]), (watch["id"], ["5", "4"]))
        search = self.requests[-1].url.params
        self.assertEqual((search["locationId"], search["radius"], search["maxPrice"]), ("15559", "30", "50"))

        changed, _ = search_watch.update_watch(
            CHAT, watch["id"], nationwide=True, remove_price_limits=True, query="lego technic"
        )
        self.assertEqual(search_watch.describe(changed), "«lego technic» · deutschlandweit")
        self.assertNotIn("locationId", self.requests[-1].url.params)
        self.assertEqual(len(search_watch.list_watches(CHAT)), 1)

    def test_radius_only_keeps_location(self):
        self.serve(page("1"), page("1"), locations=GEROLSBACH)
        watch, _ = search_watch.create_watch(CHAT, "lego", "Gerolsbach", 10)
        changed, _ = search_watch.update_watch(CHAT, watch["id"], radius_km=50, min_price=5)
        self.assertEqual(
            search_watch.describe(changed), "«lego» · 50 km um Gerolsbach - Bayern"
        )
        self.assertEqual(changed["min_price"], 5)

    def test_rejects_bad_changes_and_keeps_watch(self):
        self.serve(page("1"))
        watch, _ = search_watch.create_watch(CHAT, "lego", max_price=50)
        for kwargs in (
            {"radius_km": 20},
            {"nationwide": True, "location": "80331"},
            {"min_price": 80},
            {"query": " "},
        ):
            with self.assertRaises(search_watch.WatchError):
                search_watch.update_watch(CHAT, watch["id"], **kwargs)
        with self.assertRaises(search_watch.WatchError):
            search_watch.update_watch(CHAT, "unbekannt", query="x")
        self.assertEqual(search_watch.list_watches(CHAT), [watch])


class OverviewTest(Base):
    def test_agent_tool_leads_to_one_overview_per_watch(self):
        import main
        import tools

        self.serve(page("2", "1"), page("3"), locations=GEROLSBACH)
        tools.set_agent_chat_id(CHAT)
        reply = tools.create_search_watch(query="lego")
        self.assertIn("angelegt", reply)
        watch_id = search_watch.list_watches(CHAT)[0]["id"]
        reply = tools.update_search_watch(watch_id=watch_id, location="Gerolsbach", radius_km=20)
        self.assertIn("geändert", reply)

        bot = mock.AsyncMock()
        with mock.patch.object(main, "BOT", bot):
            asyncio.run(main.announce_watch_overviews(CHAT))
            asyncio.run(main.announce_watch_overviews(CHAT))
        bot.send_message.assert_awaited_once()
        text = bot.send_message.call_args.args[1]
        self.assertIn("20 km um Gerolsbach - Bayern", text)
        self.assertIn("/3-23-1", text)
        self.assertNotIn("/2-23-1", text)
        self.assertIn("Alle Treffer ansehen", text)

    def test_no_overview_for_deleted_watch(self):
        import main
        import tools

        self.serve(page())
        tools.set_agent_chat_id(CHAT)
        tools.create_search_watch(query="lego")
        tools.delete_search_watch(watch_id=search_watch.list_watches(CHAT)[0]["id"])
        bot = mock.AsyncMock()
        with mock.patch.object(main, "BOT", bot):
            asyncio.run(main.announce_watch_overviews(CHAT))
        bot.send_message.assert_not_awaited()

    def test_overview_text(self):
        import main

        watch = {"query": "x", "location_id": None, "max_price": None}
        hits = [
            search_watch.Hit(str(i), f"T{i}", "1 €", "Ort", "Heute", f"https://k/{i}")
            for i in range(25)
        ]
        text = main.format_watch_overview(watch, hits)
        self.assertEqual(text.count("https://k/"), main.SEARCH_WATCH_OVERVIEW_HITS)
        self.assertIn("Alle Treffer ansehen", text)
        self.assertIn("Aktuell gibt es keine Treffer", main.format_watch_overview(watch, []))


class NotificationTest(Base):
    def test_new_hits_go_to_the_owner(self):
        import main

        self.serve(page("1"), page("1"), page("2", "1"), page("1"))
        mine, _ = search_watch.create_watch(CHAT, "lego <technic>")
        search_watch.create_watch(CHAT + 1, "rennrad")
        bot = mock.AsyncMock()
        with (
            mock.patch.object(main, "BOT", bot),
            mock.patch.object(main, "ALLOWED_USER_IDS", {CHAT, CHAT + 1}),
            mock.patch.object(main, "SEARCH_WATCH_PAUSE_SECONDS", (0, 0)),
            mock.patch.object(main, "SEARCH_WATCH_MAX_JITTER_SECONDS", 0),
        ):
            asyncio.run(main.check_search_watches(None))
        bot.send_message.assert_awaited_once()
        args, kwargs = bot.send_message.call_args
        self.assertEqual(args[0], CHAT)
        self.assertIn("«lego &lt;technic&gt;»", args[1])
        self.assertIn('href="https://www.kleinanzeigen.de/s-anzeige/x/2-23-1"', args[1])
        self.assertFalse(kwargs["link_preview_options"].is_disabled)
        button = kwargs["reply_markup"].inline_keyboard[0][0]
        self.assertEqual(button.callback_data, f"wdel:{mine['id']}")

    def test_long_lists_are_shortened(self):
        import main

        watch = {"query": "x", "location_id": None, "max_price": None}
        hits = [
            search_watch.Hit(
                str(i), f"Lego Technic Set Nummer {i} komplett mit Anleitung", "1 €",
                "12345 Irgendwo (10 km)", "Heute, 12:00",
                f"https://www.kleinanzeigen.de/s-anzeige/lego-technic-set/{i}-23-1",
            )
            for i in range(60)
        ]
        text = main.format_watch_hits(watch, hits)
        self.assertLess(len(text), main.TELEGRAM_MESSAGE_LIMIT)
        self.assertTrue(text.startswith("🔔 60 neu"))
        listed = text.count("<a href=") - 1
        self.assertIn(f"… und {60 - listed} weitere", text)
        self.assertIn("Alle Treffer ansehen", text)

        text = main.format_watch_hits(watch, hits[:2], complete=False)
        self.assertTrue(text.startswith("🔔 Mehr als 2 neu"))
        self.assertIn("… und noch mehr", text)


if __name__ == "__main__":
    unittest.main()

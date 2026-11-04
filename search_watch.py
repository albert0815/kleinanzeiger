"""Search watches: look for new Kleinanzeigen results and report them.

A watch belongs to the chat that created it and is stored in
<base>/<chat_id>/search-watches.json. Each check reads the results sorted by
date, page by page until it reaches results it has seen before. Creating a
watch only remembers what is there, so that later checks report what is new.
"""

from __future__ import annotations

import datetime
import html
import json
import logging
import re
import random
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx

import ad_store

log = logging.getLogger("kleinanzeigen-agent.search_watch")

BASE_URL = "https://www.kleinanzeigen.de"
SEARCH_URL = f"{BASE_URL}/s-suchanfrage.html"
LOCATION_URL = f"{BASE_URL}/s-ort-empfehlungen.json"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
TIMEOUT = httpx.Timeout(20.0)
# Radius values the site offers.
RADII_KM = (5, 10, 20, 30, 50, 100, 150, 200)
MAX_WATCHES_PER_CHAT = 20
# Remember enough IDs that ads moving up after deletions are not reported again.
MAX_SEEN_IDS = 500
# Checks run once a day; this bounds the requests for very general searches.
MAX_PAGES = 5
PAGE_PAUSE_SECONDS = (1.0, 3.0)
# The user is told once after this many failed checks in a row.
FAILURES_BEFORE_NOTICE = 2

_lock = threading.Lock()

_ARTICLE_RE = re.compile(
    r'<article\b[^>]*\bdata-adid="(\d+)"[^>]*\bdata-href="([^"]*)"[^>]*>(.*?)</article>',
    re.S,
)
_TITLE_RE = re.compile(r"<h3\b[^>]*>\s*<a\b[^>]*>(.*?)</a>", re.S)
_PRICE_RE = re.compile(r"</h3>\s*<p\b[^>]*>.*?</p>\s*<div\b[^>]*>\s*<p\b[^>]*>(.*?)</p>", re.S)
_SPAN_RE = re.compile(r"<span\b[^>]*>(.*?)</span>", re.S)
_SVG_RE = re.compile(r"<svg\b.*?</svg>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")
# Only this list holds real results. With few results the page adds suggestions
# in "srchrslt-adtable-altads", which must not count as hits.
_RESULT_LIST_RE = re.compile(r'<ul\b[^>]*\bid="srchrslt-adtable"[^>]*>(.*?)</ul>', re.S)
_EMPTY_MARKER = 'id="saved-search-empty-result"'
# "Heute, 12:55", "Gestern, 18:38" or "02.11.2026"
_DATE_RE = re.compile(r"^(?:(?:Heute|Gestern), \d{1,2}:\d{2}|\d{2}\.\d{2}\.\d{4})$")


class WatchError(Exception):
    """A watch could not be created or checked; the message is for the user."""


@dataclass
class Hit:
    id: str
    title: str
    price: str
    location: str
    date: str
    url: str


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def _path(chat_id: int) -> Path:
    return ad_store.base_dir() / str(chat_id) / "search-watches.json"


def _load(chat_id: int) -> list[dict]:
    path = _path(chat_id)
    try:
        return json.loads(path.read_text(encoding="utf-8"))["watches"]
    except FileNotFoundError:
        return []
    except (OSError, ValueError, KeyError, TypeError):
        log.exception("[chat=%d] reading search watches failed", chat_id)
        return []


def _save(chat_id: int, watches: list[dict]) -> None:
    path = _path(chat_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"watches": watches}, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    temporary.replace(path)


def list_watches(chat_id: int) -> list[dict]:
    with _lock:
        return _load(chat_id)


def all_chat_ids() -> list[int]:
    """Chats that have stored watches."""
    base = ad_store.base_dir()
    if not base.is_dir():
        return []
    return sorted(
        int(p.parent.name)
        for p in base.glob("*/search-watches.json")
        if p.parent.name.lstrip("-").isdigit()
    )


def describe(watch: dict) -> str:
    """Short German description, e.g. «Lego Technic» bis 50 € · 20 km um 80331 München."""
    parts = [f"«{watch['query']}»"]
    if watch.get("max_price") is not None:
        parts.append(f"bis {watch['max_price']} €")
    text = " ".join(parts)
    if watch.get("location_id"):
        return f"{text} · {watch['radius_km']} km um {watch['location_name']}"
    return f"{text} · deutschlandweit"


# ---------------------------------------------------------------------------
# Kleinanzeigen access
# ---------------------------------------------------------------------------


def _client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept-Language": "de-DE,de;q=0.9"},
        timeout=TIMEOUT,
        follow_redirects=True,
    )


def resolve_location(text: str) -> tuple[str, str]:
    """Return (location_id, name) for a postcode or place name."""
    with _client() as client:
        response = client.get(LOCATION_URL, params={"query": text})
        response.raise_for_status()
        suggestions = response.json()
    for key, name in suggestions.items():
        location_id = key.lstrip("_")
        if location_id.isdigit() and location_id != "0":
            return location_id, str(name)
    raise WatchError(f"Den Ort «{text}» kenne ich nicht. Nenne bitte eine Postleitzahl.")


def nearest_radius(km: int | float) -> int:
    return min(RADII_KM, key=lambda r: (abs(r - km), r))


def search_params(watch: dict) -> dict:
    params = {"keywords": watch["query"], "sortingField": "SORTING_DATE"}
    if watch.get("location_id"):
        params["locationId"] = watch["location_id"]
        params["radius"] = str(watch["radius_km"])
    if watch.get("min_price") is not None:
        params["minPrice"] = str(watch["min_price"])
    if watch.get("max_price") is not None:
        params["maxPrice"] = str(watch["max_price"])
    return params


def _text(fragment: str) -> str:
    return " ".join(html.unescape(_TAG_RE.sub(" ", fragment)).split())


def parse_results(page: str) -> list[Hit]:
    """Read the result list of a search page, newest first.

    Paid TOP placements come first on every page, are often weeks old and have no
    date. They are left out; the same ads also appear at their normal position.
    Raises WatchError if the page has neither results nor the empty-result notice,
    e.g. after a layout change or on a block page. Reporting "nothing new" would
    hide that.
    """
    result_list = _RESULT_LIST_RE.search(page)
    if not result_list:
        if _EMPTY_MARKER in page:
            return []
        raise WatchError("Die Suchseite von Kleinanzeigen sah unerwartet aus.")
    hits = []
    for ad_id, href, body in _ARTICLE_RE.findall(result_list.group(1)):
        body = _SVG_RE.sub("", body)
        spans = [t for t in (_text(s) for s in _SPAN_RE.findall(body)) if t]
        date = next((t for t in spans if _DATE_RE.match(t)), None)
        if not date:
            continue
        title = _TITLE_RE.search(body)
        price = _PRICE_RE.search(body)
        location = spans[0] if spans[0] != date else ""
        if location and len(spans) > 1 and spans[1].startswith("("):
            location = f"{location} {spans[1]}"
        hits.append(
            Hit(
                id=ad_id,
                title=_text(title.group(1)) if title else "",
                price=_text(price.group(1)) if price else "",
                location=location,
                date=date,
                url=BASE_URL + html.unescape(href),
            )
        )
    return hits


def fetch_results(watch: dict, seen: set[str] | None = None) -> tuple[list[Hit], bool]:
    """Read results newest first. Returns (hits, complete).

    Without *seen* only the first page is read. With it, further pages follow
    until one contains a known result; complete is False if MAX_PAGES ran out
    first, so there may be more new results.
    """
    hits: list[Hit] = []
    ids: set[str] = set()
    with _client() as client:
        for page in range(1, MAX_PAGES + 1):
            params = search_params(watch)
            if page > 1:
                params["pageNum"] = str(page)
                time.sleep(random.uniform(*PAGE_PAUSE_SECONDS))
            response = client.get(SEARCH_URL, params=params)
            response.raise_for_status()
            page_hits = [h for h in parse_results(response.text) if h.id not in ids]
            hits.extend(page_hits)
            ids.update(h.id for h in page_hits)
            # Past the last page the site repeats it, so nothing new means the end.
            if seen is None or not page_hits or ids & seen:
                return hits, True
    return hits, False


def search_url(watch: dict) -> str:
    return str(httpx.URL(SEARCH_URL, params=search_params(watch)))


# ---------------------------------------------------------------------------
# Watch lifecycle
# ---------------------------------------------------------------------------


def _clean_price(value) -> int | None:
    if value is None or value == "":
        return None
    price = int(value)
    if price < 0:
        raise WatchError("Preise dürfen nicht negativ sein.")
    return price


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


def _clean_query(query: str | None) -> str:
    query = " ".join((query or "").split())
    if not query:
        raise WatchError("Der Suchbegriff fehlt.")
    if len(query) > 100:
        raise WatchError("Der Suchbegriff ist zu lang.")
    return query


def _check_prices(watch: dict) -> None:
    if (
        watch["min_price"] is not None
        and watch["max_price"] is not None
        and watch["min_price"] > watch["max_price"]
    ):
        raise WatchError("Der Mindestpreis liegt über dem Höchstpreis.")


def _set_location(watch: dict, location: str, radius_km: int | None) -> None:
    watch["location_id"], watch["location_name"] = resolve_location(location)
    watch["radius_km"] = nearest_radius(radius_km or watch.get("radius_km") or 30)


def _remember_current(watch: dict) -> list[Hit]:
    """Read the first page and treat it as known, so only later results are reported."""
    hits, _ = fetch_results(watch)
    watch["seen"] = [h.id for h in hits]
    watch["checked_at"] = _now()
    watch["failures"] = 0
    watch["failure_notified"] = False
    return hits


def create_watch(
    chat_id: int,
    query: str,
    location: str | None = None,
    radius_km: int | None = None,
    min_price: int | None = None,
    max_price: int | None = None,
) -> tuple[dict, list[Hit]]:
    """Create a watch and remember its current results. Returns (watch, current hits)."""
    watch = {
        "id": uuid.uuid4().hex[:8],
        "query": _clean_query(query),
        "location_id": None,
        "location_name": None,
        "radius_km": None,
        "min_price": _clean_price(min_price),
        "max_price": _clean_price(max_price),
        "created_at": _now(),
        "checked_at": None,
        "failures": 0,
        "failure_notified": False,
        "seen": [],
    }
    _check_prices(watch)
    with _lock:
        if len(_load(chat_id)) >= MAX_WATCHES_PER_CHAT:
            raise WatchError(
                f"Du hast schon {MAX_WATCHES_PER_CHAT} Suchaufträge. Lösche zuerst einen."
            )
    location = (location or "").strip()
    if location:
        _set_location(watch, location, radius_km)
    hits = _remember_current(watch)
    with _lock:
        watches = _load(chat_id)
        watches.append(watch)
        _save(chat_id, watches)
    log.info("[chat=%d] search watch %s created: %s", chat_id, watch["id"], describe(watch))
    return watch, hits


def update_watch(
    chat_id: int,
    watch_id: str,
    query: str | None = None,
    location: str | None = None,
    radius_km: int | None = None,
    nationwide: bool = False,
    min_price: int | None = None,
    max_price: int | None = None,
    remove_price_limits: bool = False,
) -> tuple[dict, list[Hit]]:
    """Change a watch. Arguments left at None stay as they are.

    The search is different afterwards, so its current results are read again and
    count as known. Returns (watch, current hits).
    """
    watch = next((w for w in list_watches(chat_id) if w["id"] == watch_id), None)
    if not watch:
        raise WatchError(f"Es gibt keinen Suchauftrag mit der ID {watch_id}.")
    location = (location or "").strip()
    if nationwide and (location or radius_km):
        raise WatchError("Deutschlandweit und ein Ort schließen sich aus.")
    if query is not None:
        watch["query"] = _clean_query(query)
    if remove_price_limits:
        watch["min_price"] = watch["max_price"] = None
    if min_price is not None:
        watch["min_price"] = _clean_price(min_price)
    if max_price is not None:
        watch["max_price"] = _clean_price(max_price)
    _check_prices(watch)
    if nationwide:
        watch["location_id"] = watch["location_name"] = watch["radius_km"] = None
    elif location:
        _set_location(watch, location, radius_km)
    elif radius_km:
        if not watch.get("location_id"):
            raise WatchError("Für einen Umkreis brauche ich einen Ort oder eine Postleitzahl.")
        watch["radius_km"] = nearest_radius(radius_km)
    hits = _remember_current(watch)
    with _lock:
        watches = _load(chat_id)
        index = next((i for i, w in enumerate(watches) if w["id"] == watch_id), None)
        if index is None:
            raise WatchError("Der Suchauftrag wurde inzwischen gelöscht.")
        watches[index] = watch
        _save(chat_id, watches)
    log.info("[chat=%d] search watch %s changed: %s", chat_id, watch_id, describe(watch))
    return watch, hits


# Overviews of created or changed watches, shown by the bot after the agent's reply.
_overviews: dict[int, dict[str, tuple[dict, list[Hit]]]] = {}


def store_overview(chat_id: int, watch: dict, hits: list[Hit]) -> None:
    with _lock:
        _overviews.setdefault(chat_id, {})[watch["id"]] = (watch, hits)


def pop_overviews(chat_id: int) -> list[tuple[dict, list[Hit]]]:
    with _lock:
        return list(_overviews.pop(chat_id, {}).values())


def delete_watch(chat_id: int, watch_id: str) -> dict | None:
    with _lock:
        watches = _load(chat_id)
        found = next((w for w in watches if w["id"] == watch_id), None)
        if found:
            _save(chat_id, [w for w in watches if w["id"] != watch_id])
    if found:
        log.info("[chat=%d] search watch %s deleted", chat_id, watch_id)
    return found


@dataclass
class CheckResult:
    watch: dict
    new_hits: list[Hit]
    # False if there were more new results than could be read.
    complete: bool = True
    # "failed" once the failure threshold is reached, "recovered" after that.
    notice: str | None = None
    error: str | None = None


def check_watch(chat_id: int, watch_id: str) -> CheckResult | None:
    """Look for new results of one watch. None if the watch no longer exists."""
    watch = next((w for w in list_watches(chat_id) if w["id"] == watch_id), None)
    if not watch:
        return None
    try:
        hits, complete = fetch_results(watch, set(watch.get("seen", [])))
        error = None
    except (httpx.HTTPError, ValueError, WatchError) as e:
        log.warning("[chat=%d] search watch %s failed: %s", chat_id, watch_id, e)
        hits, complete, error = None, True, str(e)

    with _lock:
        watches = _load(chat_id)
        stored = next((w for w in watches if w["id"] == watch_id), None)
        if not stored:
            return None
        result = CheckResult(stored, [], complete, error=error)
        if hits is None:
            stored["failures"] = stored.get("failures", 0) + 1
            if stored["failures"] >= FAILURES_BEFORE_NOTICE and not stored.get("failure_notified"):
                stored["failure_notified"] = True
                result.notice = "failed"
        else:
            if stored.get("failure_notified"):
                result.notice = "recovered"
            stored["failures"] = 0
            stored["failure_notified"] = False
            seen = set(stored.get("seen", []))
            result.new_hits = [h for h in hits if h.id not in seen]
            current = [h.id for h in hits]
            stored["seen"] = (current + [i for i in stored.get("seen", []) if i not in set(current)])[
                :MAX_SEEN_IDS
            ]
            stored["checked_at"] = datetime.datetime.now(datetime.UTC).isoformat(
                timespec="seconds"
            )
        _save(chat_id, watches)
    return result

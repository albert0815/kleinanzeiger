"""Capture new recipes from user input or a link, with confirmation before saving."""

from __future__ import annotations

import html
import ipaddress
import json
import logging
import re
import socket
import threading
import uuid
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

import ad_store
import mahlino

log = logging.getLogger("kleinanzeigen-agent.recipes")

MAX_PAGE_BYTES = 2_000_000
MAX_TEXT_CHARS = 12_000
MAX_REDIRECTS = 4
_TIMEOUT = 15
_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/151.0 Safari/537.36"
_LOCK = threading.Lock()
_MARKUP = re.compile(r"\*\*|__|^\s*#+\s*|`", re.MULTILINE)
_LIST_PREFIX = re.compile(r"^(\d+[.)]|-|•|\*)\s+")
_TIP_PREFIX = re.compile(r"^(unser tipp|tipp|hinweis|achtung|info)\s*:", re.IGNORECASE)
_JSON_LD = re.compile(
    r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", re.DOTALL | re.IGNORECASE
)


class RecipeError(ValueError):
    """A problem to report back in plain words."""


# ---------------------------------------------------------------------------
# Reading a recipe page
# ---------------------------------------------------------------------------


def check_url(url: str) -> None:
    """Allow only public web addresses, so a link cannot reach the home network."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise RecipeError("Das ist kein gültiger Web-Link.")
    if parts.username or parts.password or parts.port not in (None, 80, 443):
        raise RecipeError("Dieser Link ist nicht erlaubt.")
    try:
        addresses = socket.getaddrinfo(parts.hostname, None)
    except OSError as e:
        raise RecipeError("Die Adresse wurde nicht gefunden.") from e
    for address in addresses:
        if not ipaddress.ip_address(address[4][0]).is_global:
            raise RecipeError("Dieser Link führt nicht ins öffentliche Internet.")


def _download(url: str) -> tuple[str, str]:
    with httpx.Client(timeout=_TIMEOUT, headers={"User-Agent": _USER_AGENT}) as client:
        for _ in range(MAX_REDIRECTS + 1):
            check_url(url)
            with client.stream("GET", url) as resp:
                if resp.is_redirect:
                    url = urljoin(url, resp.headers.get("location", ""))
                    continue
                if resp.status_code != 200:
                    raise RecipeError(f"Die Seite antwortet mit Status {resp.status_code}.")
                if "html" not in resp.headers.get("content-type", "html").lower():
                    raise RecipeError("Der Link führt nicht auf eine Webseite.")
                body = b""
                for chunk in resp.iter_bytes():
                    body += chunk
                    if len(body) > MAX_PAGE_BYTES:
                        raise RecipeError("Die Seite ist zu groß.")
                return url, body.decode(resp.encoding or "utf-8", errors="replace")
    raise RecipeError("Der Link leitet zu oft weiter.")


def _find_recipe(node) -> dict | None:
    if isinstance(node, list):
        return next((r for r in map(_find_recipe, node) if r), None)
    if isinstance(node, dict):
        kinds = node.get("@type")
        if kinds == "Recipe" or (isinstance(kinds, list) and "Recipe" in kinds):
            return node
        return _find_recipe(node.get("@graph", []))
    return None


def extract_structured_recipe(page: str) -> dict | None:
    """Return the schema.org recipe most recipe sites embed, without the noise."""
    for block in _JSON_LD.findall(page):
        try:
            recipe = _find_recipe(json.loads(html.unescape(block).strip()))
        except ValueError:
            continue
        if recipe:
            keep = (
                "name",
                "description",
                "recipeYield",
                "prepTime",
                "cookTime",
                "totalTime",
                "recipeIngredient",
                "recipeInstructions",
            )
            return {k: recipe[k] for k in keep if k in recipe}
    return None


class _TextExtractor(HTMLParser):
    _SKIPPED = {"script", "style", "noscript", "svg", "template"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIPPED:
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in self._SKIPPED and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.parts.append(data.strip())


def page_text(page: str) -> str:
    extractor = _TextExtractor()
    extractor.feed(page)
    return "\n".join(extractor.parts)[:MAX_TEXT_CHARS]


def read_recipe_page(url: str) -> dict:
    final_url, page = _download(url.strip())
    structured = extract_structured_recipe(page)
    return {
        "source_url": final_url,
        "structured_recipe": structured,
        "page_text": None if structured else page_text(page),
    }


# ---------------------------------------------------------------------------
# Proposals: one open recipe per chat, saved only by a button press
# ---------------------------------------------------------------------------


def _path() -> Path:
    return ad_store.base_dir() / ".recipe-proposals.json"


def _load() -> dict:
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log.error("Cannot read recipe proposals: %s", e)
        return {}


def _write(proposals: dict) -> None:
    temporary = _path().with_suffix(".tmp")
    temporary.write_text(json.dumps(proposals, ensure_ascii=False, indent=1), encoding="utf-8")
    temporary.replace(_path())


def clean_instructions(text: str | None) -> tuple[str | None, str | None]:
    """Prepare instructions for the kitchen panel. Returns (instructions, link).

    The panel (RecipeScreen.kt) treats every line as one automatically numbered
    step, so own numbering and blank lines must go and markup would show up as
    is. "Tipp:" & co. become a hint box, a short line ending in ":" a heading.
    A bare link would replace the steps with an embedded web page, so links are
    kept apart for source_url.
    """
    lines, links = [], []
    for raw in (text or "").splitlines():
        line = _MARKUP.sub("", raw).strip()
        line = _LIST_PREFIX.sub("", line).strip()
        if not line:
            continue
        if line.startswith("http"):
            links.append(line.split()[0])
            continue
        lines.append(line)
    return "\n".join(lines) or None, links[0] if links else None


def _clean_ingredient(item: dict) -> dict:
    text = str(item.get("text") or "").strip()
    if not text or len(text) > 200:
        raise RecipeError(f"Ungültige Zutat: {text[:40] or item}")
    quantity = item.get("quantity")
    if quantity is not None:
        try:
            quantity = float(quantity)
        except (TypeError, ValueError):
            raise RecipeError(f"Ungültige Menge bei {text}.") from None
        if quantity <= 0:
            quantity = None
    unit = str(item.get("unit") or "").strip()[:32] or None
    note = str(item.get("note") or "").strip()[:200] or None
    return {"text": text, "quantity": quantity, "unit": unit, "note": note}


def build_recipe(
    title: str,
    ingredients: list[dict],
    instructions: str | None,
    servings: int | None,
    prep_minutes: int | None,
    cook_minutes: int | None,
    description: str | None,
    source_url: str | None,
) -> dict:
    title = (title or "").strip()
    if not title or len(title) > 200:
        raise RecipeError("Das Rezept braucht einen Titel mit höchstens 200 Zeichen.")
    if not ingredients:
        raise RecipeError("Das Rezept braucht mindestens eine Zutat.")
    for minutes in (prep_minutes, cook_minutes):
        if minutes is not None and not 0 <= minutes <= 6000:
            raise RecipeError("Die Zeitangabe ist ungültig.")
    steps, link = clean_instructions(instructions)
    source_url = source_url or link
    if source_url and (
        len(source_url) > 500 or urlsplit(source_url).scheme not in ("http", "https")
    ):
        raise RecipeError("Der Quell-Link ist ungültig.")
    return {
        "title": title,
        "description": " ".join((description or "").split())[:1000] or None,
        "servings": servings if servings and servings >= 1 else 4,
        "prep_minutes": prep_minutes,
        "cook_minutes": cook_minutes,
        "instructions": steps,
        "source_url": source_url or None,
        "ingredients": [_clean_ingredient(i) for i in ingredients],
    }


def store_proposal(chat_id: int, recipe: dict) -> dict:
    proposal = {"id": uuid.uuid4().hex[:12], "status": "open", "announced": False, "recipe": recipe}
    with _LOCK:
        proposals = _load()
        proposals[str(chat_id)] = proposal
        _write(proposals)
    return proposal


def take_unannounced(chat_id: int) -> dict | None:
    with _LOCK:
        proposals = _load()
        proposal = proposals.get(str(chat_id))
        if not proposal or proposal["announced"]:
            return None
        proposal["announced"] = True
        _write(proposals)
    return proposal


def _claim(chat_id: int, proposal_id: str) -> dict | None:
    """Take a proposal out of the store, so it can only be handled once."""
    with _LOCK:
        proposals = _load()
        proposal = proposals.get(str(chat_id))
        if not proposal or proposal["id"] != proposal_id:
            return None
        del proposals[str(chat_id)]
        _write(proposals)
    return proposal


def discard_proposal(chat_id: int, proposal_id: str) -> bool:
    return _claim(chat_id, proposal_id) is not None


def save_proposal(chat_id: int, proposal_id: str) -> dict | None:
    """Create the recipe in Mahlino. Returns None if it was already handled."""
    proposal = _claim(chat_id, proposal_id)
    if not proposal:
        return None
    try:
        return mahlino.create_recipe_sync(proposal["recipe"])
    except Exception:
        with _LOCK:
            proposals = _load()
            proposals.setdefault(str(chat_id), proposal)
            _write(proposals)
        raise


def format_steps(instructions: str) -> list[str]:
    """Show the steps the way the kitchen panel will."""
    lines, number = [], 0
    for line in instructions.splitlines():
        if _TIP_PREFIX.match(line):
            lines.append(f"💡 {line}")
        elif len(line) < 40 and line.endswith(":"):
            lines.append(f"\n{line[:-1].rstrip()}")
        else:
            number += 1
            lines.append(f"{number}. {line}")
    return lines


def format_proposal(recipe: dict, limit: int = 3500) -> str:
    times = [
        f"{label} {value} min"
        for label, value in (("Zubereitung", recipe["prep_minutes"]), ("Garen", recipe["cook_minutes"]))
        if value
    ]
    lines = [f"📖 Neues Rezept: {recipe['title']}", f"{recipe['servings']} Portionen"]
    if times:
        lines.append(", ".join(times))
    if recipe["description"]:
        lines.append(recipe["description"])
    lines.append("\nZutaten:")
    for item in recipe["ingredients"]:
        amount = " ".join(
            str(v)
            for v in (
                f"{item['quantity']:g}" if item["quantity"] else None,
                item["unit"],
            )
            if v
        )
        note = f" ({item['note']})" if item["note"] else ""
        lines.append(f"• {amount + ' ' if amount else ''}{item['text']}{note}")
    if recipe["instructions"]:
        lines.append("\nZubereitung:")
        lines.extend(format_steps(recipe["instructions"]))
    elif recipe["source_url"]:
        lines.append("\nKeine eigene Anleitung: Das Panel zeigt dafür die Quellseite.")
    if recipe["source_url"]:
        lines.append(f"\nQuelle: {recipe['source_url']}")
    text = "\n".join(lines)
    return text if len(text) <= limit else f"{text[:limit]}… (Anzeige gekürzt, gespeichert wird alles)"

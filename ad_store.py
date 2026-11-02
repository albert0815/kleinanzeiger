"""Local ad files plus the stored snapshot of what is online for an account."""

from __future__ import annotations

import datetime
import difflib
import hashlib
import json
import logging
import math
import os
import re
import shutil
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

log = logging.getLogger("kleinanzeigen-agent.store")

EXPIRY_WARNING_DAYS = 8
_STATE_LOCK = threading.Lock()


def base_dir() -> Path:
    return Path(
        os.environ.get(
            "KLEINANZEIGEN_BASE_DIR",
            "/docker-volumes/kleinanzeiger/kleinanzeigen-config",
        )
    )


@dataclass
class LocalAd:
    key: str
    path: Path
    title: str
    price: object
    active: bool
    ad_id: str | None


@dataclass
class OnlineAd:
    ad_id: str
    title: str
    state: str
    expires: str


def _ads_dir(chat_id: int) -> Path:
    return base_dir() / str(chat_id) / "ads"


def _ad_key(chat_id: int, ad_file: Path) -> str:
    relative = ad_file.relative_to(_ads_dir(chat_id)).as_posix()
    return hashlib.sha1(relative.encode("utf-8")).hexdigest()[:12]


def _load_ad(chat_id: int, ad_file: Path) -> LocalAd | None:
    try:
        data = yaml.safe_load(ad_file.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        log.warning("[chat=%d] Cannot read %s: %s", chat_id, ad_file, e)
        return None
    ad_id = data.get("id")
    return LocalAd(
        key=_ad_key(chat_id, ad_file),
        path=ad_file,
        title=str(data.get("title") or ad_file.parent.name),
        price=data.get("price", "?"),
        active=bool(data.get("active", True)),
        ad_id=str(ad_id) if ad_id else None,
    )


def list_local_ads(chat_id: int) -> list[LocalAd]:
    ads_dir = _ads_dir(chat_id)
    if not ads_dir.is_dir():
        return []
    loaded = (_load_ad(chat_id, f) for f in sorted(ads_dir.glob("*/*.yaml")))
    return [ad for ad in loaded if ad]


def find_ad(chat_id: int, key: str) -> LocalAd | None:
    return next((ad for ad in list_local_ads(chat_id) if ad.key == key), None)


def load_ad_file(chat_id: int, ad_file: Path) -> LocalAd | None:
    return _load_ad(chat_id, ad_file) if ad_file.exists() else None


def set_active(ad_file: Path, active: bool) -> None:
    value = "true" if active else "false"
    text = ad_file.read_text(encoding="utf-8")
    text, count = re.subn(r"(?m)^active:\s*\S+\s*$", f"active: {value}", text)
    if not count:
        text = f"active: {value}\n{text}"
    ad_file.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Stored state: <base>/<chat_id>/agent-state.json
# ---------------------------------------------------------------------------


def empty_state() -> dict:
    return {
        "synced_at": None,
        "online": [],
        "sold": {},
        "health": {"ok": True, "kind": None, "message": None},
        "reported": {"missing": [], "online_only": [], "expiring": []},
    }


def _state_path(chat_id: int) -> Path:
    return base_dir() / str(chat_id) / "agent-state.json"


def load_state(chat_id: int) -> dict:
    state = empty_state()
    try:
        state.update(json.loads(_state_path(chat_id).read_text(encoding="utf-8")))
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        log.error("[chat=%d] Cannot read state: %s", chat_id, e)
    return state


def save_state(chat_id: int, state: dict) -> None:
    path = _state_path(chat_id)
    temporary = path.with_suffix(".tmp")
    with _STATE_LOCK:
        temporary.write_text(
            json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        temporary.replace(path)


def record_sold(chat_id: int, ad: LocalAd) -> None:
    state = load_state(chat_id)
    state["sold"][ad.key] = {
        "title": ad.title,
        "price": ad.price,
        "ad_id": ad.ad_id,
        "sold_on": datetime.date.today().isoformat(),
    }
    save_state(chat_id, state)


# ---------------------------------------------------------------------------
# Comparing local ads with the online snapshot
# ---------------------------------------------------------------------------


def _parse_expiry(value: str) -> datetime.date | None:
    try:
        return datetime.datetime.strptime(value, "%d.%m.%Y").date()
    except ValueError:
        return None


def ad_status(ad: LocalAd, state: dict, online_by_id: dict[str, dict]) -> str:
    """One of: sold, draft, inactive, unknown, online, reserved, missing."""
    if ad.key in state["sold"]:
        return "sold"
    online = online_by_id.get(ad.ad_id or "")
    if online:
        return "reserved" if online["state"] == "reserved" else "online"
    if not ad.active:
        return "inactive"
    if not ad.ad_id:
        return "draft"
    return "missing" if state["synced_at"] else "unknown"


def overview(chat_id: int, today: datetime.date | None = None) -> dict:
    """Merge local ads and the last online snapshot into one view."""
    today = today or datetime.date.today()
    state = load_state(chat_id)
    online_by_id = {ad["ad_id"]: ad for ad in state["online"]}
    local = list_local_ads(chat_id)
    entries = []
    for ad in local:
        online = online_by_id.get(ad.ad_id or "")
        expires = online["expires"] if online else None
        expiry = _parse_expiry(expires) if expires else None
        entries.append(
            {
                "key": ad.key,
                "title": ad.title,
                "price": ad.price,
                "ad_id": ad.ad_id,
                "local_active": ad.active,
                "status": ad_status(ad, state, online_by_id),
                "expires": expires,
                "expiring_soon": bool(
                    expiry and (expiry - today).days <= EXPIRY_WARNING_DAYS
                ),
            }
        )
    local_ids = {ad.ad_id for ad in local if ad.ad_id}
    return {
        "synced_at": state["synced_at"],
        "health": state["health"],
        "ads": entries,
        "online_only": [a for a in state["online"] if a["ad_id"] not in local_ids],
    }


def apply_sync(
    chat_id: int, online: list[OnlineAd], now: datetime.datetime | None = None
) -> dict:
    """Store a fresh snapshot. Returns only what is new since the last report."""
    now = now or datetime.datetime.now(datetime.UTC)
    state = load_state(chat_id)
    first_sync = state["synced_at"] is None
    state["online"] = [asdict(ad) for ad in online]
    state["synced_at"] = now.isoformat(timespec="seconds")
    save_state(chat_id, state)

    view = overview(chat_id, now.date())
    missing = {a["key"]: a for a in view["ads"] if a["status"] == "missing"}
    online_only = {a["ad_id"]: a for a in view["online_only"]}
    expiring = {a["key"]: a for a in view["ads"] if a["expiring_soon"]}
    reported = state["reported"]
    report = {
        "first_sync": first_sync,
        "missing": [v for k, v in missing.items() if k not in reported["missing"]],
        "online_only": [
            v for k, v in online_only.items() if k not in reported["online_only"]
        ],
        "expiring": [v for k, v in expiring.items() if k not in reported["expiring"]],
    }
    state["reported"] = {
        "missing": list(missing),
        "online_only": list(online_only),
        "expiring": list(expiring),
    }
    save_state(chat_id, state)
    return report


def update_health(chat_id: int, ok: bool, kind: str | None, message: str | None) -> bool:
    """Store the account health. Returns True when ok/not-ok or the kind changed."""
    state = load_state(chat_id)
    previous = state["health"]
    changed = previous["ok"] != ok or previous["kind"] != kind
    state["health"] = {"ok": ok, "kind": kind, "message": message}
    save_state(chat_id, state)
    return changed


# ---------------------------------------------------------------------------
# Republishing: what is due, and refreshing local data from the online ad
# ---------------------------------------------------------------------------

# Everything else (id, dates, counters) stays local.
_FIELDS_FROM_ONLINE = (
    "type",
    "title",
    "description",
    "category",
    "special_attributes",
    "price",
    "price_type",
    "shipping_type",
    "shipping_costs",
    "shipping_options",
    "sell_directly",
    "contact",
)


def _last_published(ad_file: Path) -> datetime.datetime:
    data = yaml.safe_load(ad_file.read_text(encoding="utf-8")) or {}
    dates = []
    for value in (data.get("created_on"), data.get("updated_on")):
        try:
            parsed = datetime.datetime.fromisoformat(str(value))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.UTC)
        dates.append(parsed)
    return max(dates, default=datetime.datetime.min.replace(tzinfo=datetime.UTC))


def select_for_republish(
    chat_id: int,
    online: list[OnlineAd],
    interval_days: int,
    now: datetime.datetime | None = None,
) -> list[LocalAd]:
    """Pick today's ads: online, active, not sold, due. Oldest first.

    Spreads the account over the interval, so ads reappear staggered and the
    monthly quota is not hit in one go.
    """
    now = now or datetime.datetime.now(datetime.UTC)
    online_active = {ad.ad_id for ad in online if ad.state == "active"}
    sold = load_state(chat_id)["sold"]
    eligible = [
        ad
        for ad in list_local_ads(chat_id)
        if ad.active and ad.ad_id in online_active and ad.key not in sold
    ]
    daily_limit = math.ceil(len(eligible) / interval_days)
    published = {ad.key: _last_published(ad.path) for ad in eligible}
    due = [
        ad
        for ad in eligible
        if (now - published[ad.key]).days >= interval_days
    ]
    due.sort(key=lambda ad: published[ad.key])
    return due[:daily_limit]


def find_download(chat_id: int, ad_id: str) -> Path | None:
    folder = base_dir() / str(chat_id) / "downloaded-ads"
    return next(iter(sorted(folder.glob(f"ad_{ad_id}_*/ad_{ad_id}.yaml"))), None)


def import_download(chat_id: int, downloaded: Path) -> LocalAd | None:
    """Make a downloaded ad a regular local ad. Existing folders stay untouched."""
    target = _ads_dir(chat_id) / downloaded.parent.name
    if target.exists():
        log.warning("[chat=%d] Not importing, %s already exists", chat_id, target)
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(downloaded.parent), str(target))
    return _load_ad(chat_id, target / downloaded.name)


def _local_images(ad_file: Path, data: dict) -> list[Path]:
    found: set[Path] = set()
    for pattern in data.get("images") or []:
        found.update(p for p in ad_file.parent.glob(str(pattern)) if p.is_file())
    return sorted(found)


def merge_online_data(ad_file: Path, downloaded: Path) -> list[str]:
    """Update a local ad with its online data. Returns the changed field names.

    Local images are kept when their number matches: the downloaded ones are
    recompressed, so re-uploading them each time would lower the quality.
    """
    local = yaml.safe_load(ad_file.read_text(encoding="utf-8")) or {}
    remote = yaml.safe_load(downloaded.read_text(encoding="utf-8")) or {}
    if not remote.get("title") or not remote.get("description"):
        raise ValueError(f"Unvollständige Online-Daten: {downloaded}")

    changed = [f for f in _FIELDS_FROM_ONLINE if local.get(f) != remote.get(f)]
    for field_name in changed:
        local[field_name] = remote.get(field_name)
    # The online description already includes any prefix or suffix.
    local["description_prefix"] = ""
    local["description_suffix"] = ""

    images = _local_images(ad_file, local)
    remote_images = [downloaded.parent / str(name) for name in remote.get("images") or []]
    if remote_images and len(images) != len(remote_images):
        for image in images:
            image.unlink()
        for image in remote_images:
            shutil.copy2(image, ad_file.parent / image.name)
        local["images"] = [image.name for image in remote_images]
        changed.append("images")

    temporary = ad_file.with_suffix(".tmp")
    temporary.write_text(
        yaml.safe_dump(local, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    temporary.replace(ad_file)
    return changed


# ---------------------------------------------------------------------------
# Categories known to the bot
# ---------------------------------------------------------------------------

_CATEGORY_ID = re.compile(r"^\d+(/\w+)+$")
_categories: list[str] | None = None


def _load_categories() -> list[str]:
    global _categories
    if _categories is None:
        path = Path(
            os.environ.get(
                "KLEINANZEIGEN_CATEGORIES_FILE", "/app/resources/categories.yaml"
            )
        )
        try:
            _categories = list(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
        except (OSError, yaml.YAMLError) as e:
            log.error("Cannot load categories: %s", e)
            _categories = []
    return _categories


def resolve_category(name: str) -> str | None:
    """Map a suggested category to one the bot knows, or None if nothing fits."""
    name = name.strip()
    if _CATEGORY_ID.match(name):
        return name
    categories = _load_categories()
    by_lower = {c.lower(): c for c in categories}
    if name.lower() in by_lower:
        return by_lower[name.lower()]
    matches = difflib.get_close_matches(name, categories, n=1, cutoff=0.7)
    return matches[0] if matches else None


def search_categories(query: str, limit: int = 15) -> list[str]:
    words = query.lower().split()
    categories = _load_categories()
    found = [c for c in categories if all(w in c.lower() for w in words)]
    return found[:limit] or difflib.get_close_matches(
        query, categories, n=limit, cutoff=0.4
    )

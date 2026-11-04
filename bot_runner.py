"""Single adapter around the kleinanzeigen-bot CLI.

Every invocation for an account runs under that account's lock, because all
commands share one browser profile. Output parsing lives here too, so an
upstream format change only affects this module.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import signal
from dataclasses import dataclass
from pathlib import Path

from ad_store import OnlineAd, base_dir

log = logging.getLogger("kleinanzeigen-agent.bot")

DEFAULT_TIMEOUT_SECONDS = 600
LIST_TIMEOUT_SECONDS = 180
_TRANSIENT_BROWSER_ERROR = "Failed to start browser"
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_LOCKS: dict[int, asyncio.Lock] = {}

_LIST_ENTRY = re.compile(
    r"^ID: (?P<id>\d+)\n"
    r"\s+title: (?P<title>.*)\n"
    r"\s+status: (?P<state>.*)\n"
    r"\s+expires: (?P<expires>.*)$",
    re.MULTILINE,
)
_INCOMPLETE_LIST = (
    "Pagination request failed",
    "Unexpected pagination response",
    "Stopping pagination",
)


class BotFailure(Exception):
    """A bot run failed. `kind` is one of: login, browser, timeout, config, other."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


@dataclass
class BotResult:
    returncode: int
    output: str


def classify_failure(output: str) -> str:
    if re.search(
        r"ID 'username'|captcha|\bSMS\b|device verification|Login failed", output, re.I
    ):
        return "login"
    if _TRANSIENT_BROWSER_ERROR in output:
        return "browser"
    if "TimeoutError" in output:
        return "timeout"
    return "other"


_REASON_PATTERNS = (
    re.compile(r"Form rejected for \[[^\]]*\]: (?:Ad form validation failed: )?(?P<reason>.+?)(?:\. Correct the ad configuration.*)?$", re.M),
    re.compile(r"Category resolution failed for \[[^\]]*\]: (?P<reason>.+?)$", re.M),
    re.compile(r"(?P<reason>Option '.+?' not found in button combobox '[^']+')", re.M),
    re.compile(r"(?P<reason>Failed to set attribute '[^']+')", re.M),
)
_REASON_LABELS = {"Bitte gib einen Wert ein": "Pflichtfeld fehlt"}


def failure_reasons(output: str) -> dict[str, str]:
    """Reasons per ad title from a publish run, e.g. a rejected required field."""
    text = _ANSI.sub("", output)
    reasons: dict[str, str] = {}
    for line in text.splitlines():
        title = re.search(r"for \[([^\]]*)\]", line)
        for pattern in _REASON_PATTERNS:
            match = pattern.search(line)
            if match:
                reason = match["reason"].strip().rstrip(".")
                reason = re.sub(r"Offered choices: (.{120}).*", r"Offered choices: \1…", reason)
                for raw, label in _REASON_LABELS.items():
                    reason = reason.replace(raw, label)
                reasons.setdefault(title[1] if title else "", reason)
                break
    return reasons


def is_permanent_failure(output: str) -> bool:
    """The site or the bot rejected the ad itself; retrying unchanged cannot help."""
    text = _ANSI.sub("", output)
    return any(marker in text for marker in (
        "Form rejected for", "Category resolution failed", "not found in button combobox",
        "Failed to set attribute", "Correct the ad configuration",
    ))


def parse_list_output(output: str) -> list[OnlineAd]:
    """Parse `kleinanzeigen-bot --lang=en list` output.

    Raises BotFailure when the list may be incomplete: treating a partial list
    as the truth would report online ads as gone.
    """
    text = _ANSI.sub("", output)
    for marker in _INCOMPLETE_LIST:
        if marker in text:
            raise BotFailure("other", f"Online-Liste unvollständig: {marker}")
    ads = [
        OnlineAd(
            ad_id=m["id"],
            title=m["title"].strip(),
            state=m["state"].strip(),
            expires=m["expires"].strip(),
        )
        for m in _LIST_ENTRY.finditer(text)
    ]
    if not ads and "No online ads found" not in text:
        raise BotFailure("other", "Ausgabe von 'list' konnte nicht gelesen werden.")
    return ads


def _config_path(chat_id: int) -> Path:
    return base_dir() / str(chat_id) / "config.yaml"


def clear_browser_locks(cfg_path: Path) -> None:
    """Remove Chromium lock files left by a dead process or an old container.

    Only call this while holding the account lock, when no bot run is active.
    """
    profile_dir = cfg_path.parent / ".temp" / "browser-profile"
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            (profile_dir / name).unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            log.warning("Could not remove browser lock %s: %s", name, e)


async def _run_once(cmd: list[str], cwd: Path, timeout: float) -> BotResult:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except (TimeoutError, asyncio.CancelledError) as e:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await proc.wait()
        if isinstance(e, asyncio.CancelledError):
            raise
        raise BotFailure(
            "timeout", f"Der Bot hat nach {int(timeout)} s nicht geantwortet."
        ) from e
    return BotResult(proc.returncode or 0, stdout.decode("utf-8", errors="replace"))


async def run_bot(
    chat_id: int,
    *args: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    attempts: int = 3,
) -> BotResult:
    """Run one bot command for an account, retrying browser start races."""
    command = os.environ.get("KLEINANZEIGEN_BOT_CMD", "")
    if not command:
        raise BotFailure("config", "KLEINANZEIGEN_BOT_CMD ist nicht konfiguriert.")
    cfg_path = _config_path(chat_id)
    if not cfg_path.exists():
        raise BotFailure("config", f"Konfigurationsdatei nicht gefunden: {cfg_path}")

    cmd = shlex.split(command) + [
        "--workspace-mode=portable",
        "--lang=en",
        f"--config={cfg_path}",
        *args,
    ]
    lock = _LOCKS.setdefault(chat_id, asyncio.Lock())
    async with lock:
        result = BotResult(0, "")
        for attempt in range(1, attempts + 1):
            clear_browser_locks(cfg_path)
            log.info("[chat=%d] Running: %s", chat_id, " ".join(args))
            result = await _run_once(cmd, cfg_path.parent, timeout)
            if result.returncode == 0 or _TRANSIENT_BROWSER_ERROR not in result.output:
                break
            log.warning(
                "[chat=%d] Browser start failed (%d/%d)", chat_id, attempt, attempts
            )
            await asyncio.sleep(2)
    return result


async def list_online(chat_id: int) -> list[OnlineAd]:
    result = await run_bot(chat_id, "list", timeout=LIST_TIMEOUT_SECONDS)
    if result.returncode != 0:
        log.error("[chat=%d] list failed:\n%s", chat_id, result.output)
        raise BotFailure(
            classify_failure(result.output),
            f"Online-Inserate konnten nicht gelesen werden (rc={result.returncode}).",
        )
    return parse_list_output(result.output)


async def publish_new(chat_id: int) -> BotResult:
    return await run_bot(chat_id, "publish", "--ads=new")


async def publish_ids(chat_id: int, ad_ids: list[str]) -> BotResult:
    return await run_bot(chat_id, "publish", f"--ads={','.join(ad_ids)}")


async def download_ids(chat_id: int, ad_ids: list[str]) -> BotResult:
    """Fetch the online data into the separate downloaded-ads folder."""
    return await run_bot(chat_id, "download", f"--ads={','.join(ad_ids)}")


FIELDS_PREFIX = "CATEGORY_FIELDS_JSON "


async def category_fields(chat_id: int, category: str) -> list[dict]:
    """Fields of a category via the patched `fields` command. Never submits."""
    import json

    result = await run_bot(chat_id, "fields", f"--category={category}", timeout=300)
    line = next(
        (l for l in _ANSI.sub("", result.output).splitlines() if l.startswith(FIELDS_PREFIX)), None
    )
    if result.returncode != 0 or line is None:
        log.error("[chat=%d] fields failed (rc=%d):\n%s", chat_id, result.returncode, result.output)
        reasons = failure_reasons(result.output)
        raise BotFailure(
            classify_failure(result.output),
            next(iter(reasons.values()), "Die Felder der Kategorie konnten nicht gelesen werden."),
        )
    return json.loads(line[len(FIELDS_PREFIX):])["fields"]


_FIELD_ERROR = re.compile(r"(?P<label>[^;:\[\]]+?) \[(?P<key>[\w.]+)\]: (?P<message>[^;]+)")
_OPTION_ERROR = re.compile(
    r"Option '(?P<value>.*?)' not found in button combobox '(?P<key>[\w.]+)'.*?\(observed: \[(?P<options>.*?)\]\)"
)
_UNKNOWN_FIELD = re.compile(r"Failed to set attribute '(?P<attribute>[^']+)'")


def field_problems(output: str) -> list[dict]:
    """Field problems from a failed publish, using the patched error messages.

    Kinds: "missing" (key, label, message), "invalid" (key, value, options) and
    "unknown" (attribute that this category does not have).
    """
    text = _ANSI.sub("", output)
    problems: list[dict] = []
    seen: set = set()

    def add(problem: dict) -> None:
        ident = (problem["kind"], problem.get("key") or problem.get("attribute"))
        if ident not in seen:
            seen.add(ident)
            problems.append(problem)

    for line in text.splitlines():
        if "Form rejected for" in line:
            details = line.split("validation failed: ", 1)[-1]
            details = re.split(r"\.?\s*Correct the ad configuration", details)[0]
            for match in _FIELD_ERROR.finditer(details):
                add({"kind": "missing", "key": match["key"], "label": match["label"].strip(),
                     "message": match["message"].strip().rstrip(".")})
        for match in _OPTION_ERROR.finditer(line):
            options = [o.strip().strip("'\"") for o in match["options"].split(",")]
            add({"kind": "invalid", "key": match["key"], "value": match["value"],
                 "options": [o for o in options if o and o != "Bitte wählen"]})
        for match in _UNKNOWN_FIELD.finditer(line):
            add({"kind": "unknown", "attribute": match["attribute"]})
    return problems


async def delete_online(chat_id: int, ad_id: str) -> BotResult:
    return await run_bot(chat_id, "delete", f"--ads={ad_id}")

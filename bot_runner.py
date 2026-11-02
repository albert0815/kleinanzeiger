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


async def delete_online(chat_id: int, ad_id: str) -> BotResult:
    return await run_bot(chat_id, "delete", f"--ads={ad_id}")

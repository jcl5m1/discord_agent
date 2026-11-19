"""Claude usage limits: wait for the reset, then resume the agents that were cut off.

The Claude CLI reports the account's limits in its stream:

    {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed" | "allowed_warning" | "rejected",
     "resetsAt": <unix time>, "rateLimitType": "five_hour", ...}}

and, when a turn is refused, says so in the reply ("Claude AI usage limit
reached|<unix time>", or "You've hit your limit · resets 9pm"). The provider
turns that into a "limit" event with the reset time. Then the harness:

- records the cut-off turn here (data/usage_limit.json, so it survives a restart),
- makes every Claude turn wait until RESUME_AFTER seconds after the reset (the
  limit is per account), and holds back worklog offers meanwhile,
- gives each cut-off channel a resume turn at that time.

If the reset time is unknown, it tries again after RETRY_UNKNOWN seconds.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta

from . import config

PATH = config.DATA_DIR / "usage_limit.json"
RESUME_AFTER = 60  # seconds after the reset
RETRY_UNKNOWN = 30 * 60  # wait this long when the reset time is unknown
PREVIEW = 600

_EPOCH = re.compile(r"limit reached\s*\|\s*(\d{10})", re.IGNORECASE)
_CLOCK = re.compile(r"resets\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap]m)\b", re.IGNORECASE)
_LIMIT_TEXT = re.compile(r"usage limit reached|hit your (?:usage |session |weekly )?limit|limit reached\s*\|"
                         r"|out of (?:extra )?usage", re.IGNORECASE)


def reset_from_text(text: str, now: float | None = None) -> float | None:
    """The reset time a limit message names, as a unix time (None if it names none)."""
    m = _EPOCH.search(text or "")
    if m:
        return float(m.group(1))
    m = _CLOCK.search(text or "")
    if not m:
        return None
    hour = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "pm" else 0)
    base = datetime.fromtimestamp(now or time.time())
    at = base.replace(hour=hour, minute=int(m.group(2) or 0), second=0, microsecond=0)
    if at.timestamp() <= base.timestamp():
        at += timedelta(days=1)
    return at.timestamp()


def is_limit_text(text: str) -> bool:
    return bool(_LIMIT_TEXT.search(text or ""))


def _load() -> dict:
    try:
        return json.loads(PATH.read_text())
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(PATH)


def hit(reset_at: float | None, channel_id: int, instance: str, prompt: str,
        message_id: int | None = None, now: float | None = None) -> float:
    """Record a turn the limit cut off. Returns when the agents resume."""
    now = now or time.time()
    data = _load()
    current = data.get("resume_at", 0) if data.get("resume_at", 0) > now else 0
    if reset_at:  # the account's reset time: it wins over a guess
        data["resume_at"] = reset_at + RESUME_AFTER
        data["known"] = True
    elif not current:
        data["resume_at"], data["known"] = now + RETRY_UNKNOWN, False
    data.setdefault("channels", {})[str(channel_id)] = {
        "instance": instance, "started": now, "message_id": message_id, "prompt": prompt[:PREVIEW]}
    _save(data)
    return data["resume_at"]


def resume_at(now: float | None = None) -> float | None:
    """When Claude turns may run again, if they must wait now."""
    at = _load().get("resume_at")
    return at if at and at > (now or time.time()) else None


def take(channel_id: int | None = None) -> dict[int, dict]:
    """The cut-off turns to resume (all, or one channel's), and forget them."""
    data = _load()
    chans = data.get("channels", {})
    if channel_id is None:
        out, data["channels"] = chans, {}
    else:
        rec = chans.pop(str(channel_id), None)
        out = {str(channel_id): rec} if rec else {}
    if not data.get("channels"):
        data.pop("resume_at", None)
        data.pop("known", None)
    _save(data)
    return {int(k): v for k, v in out.items()}


def prompt(record: dict) -> str:
    started = time.strftime("%H:%M", time.localtime(record.get("started") or 0))
    return (
        f"[usage limit] The Claude usage limit stopped your last turn at {started}. The limit has reset "
        "now. Your session is kept, so you remember the work. Check the state of the work (wl status and "
        "your claims, files, git, background jobs), then continue it where it stopped. Re-claim worklog "
        "tasks that dropped. If the work was already finished, say so in one line.\n\n"
        f"The request of the stopped turn was:\n{(record.get('prompt') or '').strip()}"
    )


def pending() -> bool:
    """Are there cut-off turns waiting to resume?"""
    return bool(_load().get("channels"))

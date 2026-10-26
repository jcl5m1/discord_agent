"""Independent watchdog: alerts in Discord when the harness dies or a turn stalls.

Runs as a separate process (run.sh starts it alongside the bot) so it can still
report when the harness itself has crashed or wedged. The bot rewrites
data/heartbeat.json every HEARTBEAT_INTERVAL seconds; this process checks that
file every WATCHDOG_CHECK_INTERVAL seconds and posts to Discord through the
REST API with the bot token (no gateway connection needed):

- heartbeat stale (> WATCHDOG_STALE_AFTER)  → "harness is down" alert, once,
  then a "harness is back" message when heartbeats resume.
- a turn with no events for > WATCHDOG_STALL_AFTER while the harness is alive
  → "agent may be hung" warning, once per turn.

Alerts go to channels with an active turn, falling back to the channel that
last saw user activity. Long-turn "still working" updates while the harness is
healthy are posted by the bot itself (STATUS_UPDATE_INTERVAL), not here.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request

from . import config

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("discord_agent.watchdog")


def _fmt_age(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 120:
        return f"{seconds}s"
    if seconds < 7200:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def _post(channel_id: int, content: str) -> None:
    req = urllib.request.Request(
        f"https://discord.com/api/v10/channels/{channel_id}/messages",
        data=json.dumps({"content": content}).encode(),
        headers={
            "Authorization": f"Bot {config.BOT_TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15):
        pass


def _post_all(channel_ids: list[int], content: str) -> None:
    for channel_id in channel_ids:
        try:
            _post(channel_id, content)
            log.info("alert posted to channel %s: %.120s", channel_id, content)
        except Exception:
            log.exception("failed to post alert to channel %s", channel_id)


def _alert_channels(payload: dict) -> list[int]:
    channels = [t["channel_id"] for t in payload.get("turns", []) if t.get("channel_id")]
    if not channels and payload.get("last_channel_id"):
        channels = [payload["last_channel_id"]]
    return channels


def main() -> None:
    if not config.BOT_TOKEN:
        raise SystemExit("DISCORD_BOT_TOKEN is not set — copy .env.example to .env and fill it in")
    log.info(
        "watchdog started: check every %ds, harness down after %ds stale, "
        "turn stalled after %ds silent",
        config.WATCHDOG_CHECK_INTERVAL, config.WATCHDOG_STALE_AFTER,
        config.WATCHDOG_STALL_AFTER,
    )
    harness_down = False
    seen_heartbeat = False
    stall_warned: set[int] = set()
    while True:
        time.sleep(config.WATCHDOG_CHECK_INTERVAL)
        now = time.time()
        try:
            payload = json.loads(config.HEARTBEAT_PATH.read_text())
            age = now - payload["ts"]
            seen_heartbeat = True
        except FileNotFoundError:
            # Harness has never written a heartbeat (not started yet, or the
            # file was removed). Only meaningful once we've seen one before.
            if seen_heartbeat:
                log.error("heartbeat file %s disappeared", config.HEARTBEAT_PATH)
            continue
        except Exception:
            log.exception("could not read heartbeat file")
            continue

        if age > config.WATCHDOG_STALE_AFTER:
            if not harness_down:
                harness_down = True
                log.error("harness heartbeat stale for %.0fs — treating as down", age)
                _post_all(
                    _alert_channels(payload),
                    f"🔴 **harness appears down** — no heartbeat for {_fmt_age(age)} "
                    f"(pid {payload.get('pid')}). Any turn in progress is stuck; "
                    "restart with `./run.sh`. Sessions are kept and will resume.",
                )
            continue

        if harness_down:
            harness_down = False
            log.info("harness heartbeat resumed")
            _post_all(
                _alert_channels(payload),
                "🟢 **harness is back** — heartbeats resumed. "
                "Send a message in a channel to resume its session.",
            )

        stall_warned = {
            cid for cid in stall_warned
            if any(t.get("channel_id") == cid for t in payload.get("turns", []))
        }
        for turn in payload.get("turns", []):
            channel_id = turn.get("channel_id")
            silent = now - turn.get("last_event_at", payload["ts"])
            if silent > config.WATCHDOG_STALL_AFTER and channel_id not in stall_warned:
                stall_warned.add(channel_id)
                log.error(
                    "turn in channel %s (%s) silent for %.0fs",
                    channel_id, turn.get("instance"), silent,
                )
                _post_all(
                    [channel_id],
                    f"⚠️ **{turn.get('instance')}** may be hung — no agent output for "
                    f"{_fmt_age(silent)} (turn running "
                    f"{_fmt_age(now - turn.get('started_at', payload['ts']))}, "
                    f"{turn.get('tools', 0)} tool calls, model `{turn.get('model')}`). "
                    "The harness is alive; the agent subprocess is not producing output.",
                )


if __name__ == "__main__":
    main()

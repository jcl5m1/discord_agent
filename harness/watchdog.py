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

- a disk (the root disk, the harness data's disk, DISK_PATHS) rises to warn
  (90% used), critical (95%) or full (under 500 MB free) → one alert per rise,
  with the biggest folders in the home folder; "back to normal" under 85%.
  The bot can't report this itself once the disk is full.
- system health (data/health.json, from the uptime monitor) rises to `warn`
  or `critical` → one alert per rise, with the reasons and the top memory
  users, and the actions the monitor took. (The bot tells the health agent to
  act; see harness/health.py.)

Alerts go to channels with an active turn, falling back to the channel that
last saw user activity. Long-turn "still working" updates while the harness is
healthy are posted by the bot itself (STATUS_UPDATE_INTERVAL), not here.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request

from . import config, disk, health, logfile

logfile.setup("watchdog")
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


def _check_disks(levels: dict[str, str], channels: list[int]) -> None:
    """Alert when a disk's level rises (warn 90%, critical 95%, full), and when it is back under 85%."""
    for path in disk.paths():
        try:
            pct, free = disk.usage(path)
        except OSError:
            continue
        prev = levels.get(str(path), "ok")
        level = disk.level(pct, free, prev)
        levels[str(path)] = level
        if level == prev:
            continue
        rose = disk.LEVELS.index(level) > disk.LEVELS.index(prev)
        if rose:
            icon = {"warn": "🟠", "critical": "🔴", "full": "🛑"}[level]
            big = "".join(f"\n• {disk.fmt_bytes(size)} `{name}`" for size, name in disk.biggest_dirs(path))
            text = (f"{icon} **disk {level}**: `{path}` is {pct:.0f}% used, {disk.fmt_bytes(free)} free."
                    + (" Agents, the bot and the logs stop working when it is full." if level != "full" else
                       " It is full: agents can't save work, and the bot can't log or save its state.")
                    + (f"\nBiggest folders in the home folder:{big}" if big else "")
                    + "\nFree space now: delete or move old data, caches, builds or Docker images "
                      "(`docker system df`).")
        elif level == "ok":
            text = f"🟢 **disk back to normal**: `{path}` is {pct:.0f}% used, {disk.fmt_bytes(free)} free."
        else:
            continue  # going down from critical to warn: no message
        log.warning("disk %s: %s %.1f%% used, %d bytes free", level, path, pct, free)
        _post_all(channels, text[:1900])


def _check_health(prev: str, now: float) -> str:
    """Alert once when system health rises to warn or critical; returns the level now."""
    state = health.read_state()
    if not state or now - state.get("ts", 0) > 300:
        return prev  # the uptime monitor isn't writing it
    level = state.get("level", "ok")
    if health.LEVELS.index(level) > health.LEVELS.index(prev):
        top = ", ".join(f"{(p['rss_mib'] + p['swap_mib']) / 1024:.1f} GB `{p['cmd'][:50]}`"
                        for p in state.get("top_mem", [])[:3])
        acts = "".join(f"\n• {a['text'][:150]}" for a in state.get("actions", [])[-2:]
                       if now - a["ts"] < 600)
        icon = "🔴" if level == "critical" else "🟠"
        text = (f"{icon} **system health {level}**: {'; '.join(state.get('reasons', []))}. "
                f"Top memory: {top}." + (f"\nActions taken:{acts}" if acts else "")
                + "\nThe health agent is told to take preventive measures.")
        try:
            payload = json.loads(config.HEARTBEAT_PATH.read_text())
            channels = [payload["last_channel_id"]] if payload.get("last_channel_id") else []
        except (OSError, ValueError):
            channels = []
        _post_all(channels, text[:1900])
        log.warning("health %s: %s", level, state.get("reasons"))
    return level


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
    health_level = "ok"
    disk_levels: dict[str, str] = {}
    last_channels: list[int] = []
    stall_warned: set[int] = set()
    while True:
        time.sleep(config.WATCHDOG_CHECK_INTERVAL)
        now = time.time()
        health_level = _check_health(health_level, now)
        try:
            last_channels = _alert_channels(json.loads(config.HEARTBEAT_PATH.read_text())) or last_channels
        except (OSError, ValueError):
            pass
        _check_disks(disk_levels, last_channels)
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
                    "restart with `./run.sh`. Sessions are kept and will resume."
                    + ("\n🛑 A disk is full: that is the likely cause. Free space first."
                       if "full" in disk_levels.values() else ""),
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

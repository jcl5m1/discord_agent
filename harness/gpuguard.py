"""GPU watch: notice a lost GPU, tell everyone, and reboot to bring it back (part of the health checks).

The RTX 3090 here sometimes falls off the PCIe bus (NVIDIA Xid 79, "Node Reboot Required"). Only a
reboot brings it back, and while it is gone, processes that touch it hang one by one until the
machine stops answering. So, in the uptime monitor, every CHECK_EVERY seconds:

1. Ask the driver: `nvidia-smi -L`. If it can't see the GPU for CONFIRM seconds, the GPU is lost
   (a `gpu_lost` event in uptime.jsonl).
2. Post an alert in Discord and send every agent a worklog message: save your work, the machine
   reboots in GPU_REBOOT_DELAY seconds (default 5 min).
3. After the delay, check again (the GPU may be back); then `sudo -n /usr/bin/systemctl reboot`
   (the user allowed exactly that command in /etc/sudoers.d). At most one automatic reboot per
   GPU_REBOOT_MIN_GAP seconds (default 2 h), so a GPU that keeps failing can't cause a reboot loop;
   past that limit, or without the permission, it alerts every hour instead.

GPU_AUTO_REBOOT=0 turns the reboot off (the alerts stay).
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable

from . import config

log = logging.getLogger("gpuguard")

STATE = config.DATA_DIR / "gpu" / "guard.json"
CHECK_EVERY = 60.0
CONFIRM = 120.0  # lost this long before acting (one failed check is not enough)
ALERT_EVERY = 3600.0
WL = config.BASE_DIR / "bin" / "wl"


def gpu_ok() -> bool:
    """Does the driver see a GPU? A hang counts as lost (a lost GPU can block calls)."""
    try:
        r = subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return False
    except OSError:
        return True  # no nvidia-smi: nothing to watch
    return r.returncode == 0 and "GPU " in r.stdout


def _load() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def _save(state: dict) -> None:
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(state))
    except OSError:
        pass


def may_reboot() -> bool:
    """Is `sudo -n /usr/bin/systemctl reboot` allowed without a password?"""
    try:
        r = subprocess.run(["sudo", "-n", "-l", "/usr/bin/systemctl", "reboot"], capture_output=True, timeout=10)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _discord(text: str) -> None:
    """Post in the last active channel (as the watchdog does: REST, no gateway)."""
    try:
        from . import watchdog
        payload = json.loads(config.HEARTBEAT_PATH.read_text())
        channels = watchdog._alert_channels(payload)
        watchdog._post_all(channels[:1] or channels, text[:1900])
    except Exception:
        log.exception("gpuguard: could not post to Discord")


def _tell_agents(text: str) -> None:
    try:
        subprocess.run([str(WL), "--as", f"harness@{_host()}", "send", "all", text],
                       capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _host() -> str:
    import socket
    return socket.gethostname().split(".")[0]


def decide(lost_for: float, state: dict, now: float, allowed: bool) -> str:
    """What to do now: 'wait' (not lost long enough), 'reboot', or 'alert' (can't or mustn't reboot)."""
    if lost_for < CONFIRM:
        return "wait"
    if not config.GPU_AUTO_REBOOT or not allowed:
        return "alert"
    if now - state.get("last_reboot", 0) < config.GPU_REBOOT_MIN_GAP:
        return "alert"
    return "reboot"


async def run(stop: asyncio.Event, append_event: Callable[..., dict] | None = None) -> None:
    if not shutil.which("nvidia-smi") or not config.GPU_MONITOR:
        return
    log.info("gpuguard: watching the GPU every %ds (auto reboot %s)", CHECK_EVERY,
             "on" if config.GPU_AUTO_REBOOT else "off")
    lost_since: float | None = None
    last_alert = 0.0
    while not stop.is_set():
        ok = await asyncio.to_thread(gpu_ok)
        now = time.time()
        if ok:
            if lost_since and append_event:
                append_event("gpu_back", lost_since=lost_since)
            lost_since = None
        else:
            if lost_since is None:
                lost_since = now
                log.warning("gpuguard: the driver can't see the GPU")
                if append_event:
                    append_event("gpu_lost", ts=now)
            state = _load()
            what = decide(now - lost_since, state, now, await asyncio.to_thread(may_reboot))
            if what == "alert" and now - last_alert > ALERT_EVERY:
                last_alert = now
                why = ("automatic reboot is off (GPU_AUTO_REBOOT=0)" if not config.GPU_AUTO_REBOOT else
                       "the last automatic reboot was less than "
                       f"{config.GPU_REBOOT_MIN_GAP / 3600:.0f} h ago" if now - state.get("last_reboot", 0)
                       < config.GPU_REBOOT_MIN_GAP else "there is no permission to reboot")
                await asyncio.to_thread(_discord, f"🛑 **GPU lost** on {_host()} since <t:{int(lost_since)}:t>: "
                                                  f"only a reboot brings it back, and {why}. Reboot the machine.")
            elif what == "reboot":
                delay = config.GPU_REBOOT_DELAY
                at = now + delay
                await asyncio.to_thread(_discord, f"🛑 **GPU lost** on {_host()} since <t:{int(lost_since)}:t>. "
                                                  f"Only a reboot brings it back: the machine reboots at <t:{int(at)}:t>. "
                                                  "Agents are told to save their work; their turns resume after it.")
                await asyncio.to_thread(_tell_agents, (
                    f"The GPU fell off the bus. This machine reboots automatically in {delay // 60:.0f} min "
                    f"(at {time.strftime('%H:%M', time.localtime(at))}). Save and commit your work now. "
                    "Cut-off turns resume by themselves; restart background jobs after it."))
                try:
                    await asyncio.wait_for(stop.wait(), delay)
                    return  # the monitor is stopping (maybe for the reboot itself)
                except asyncio.TimeoutError:
                    pass
                if await asyncio.to_thread(gpu_ok):
                    await asyncio.to_thread(_discord, "🟢 The GPU answers again: the reboot is cancelled.")
                    lost_since = None
                    continue
                _save({**state, "last_reboot": time.time()})
                if append_event:
                    append_event("auto_reboot", reason="gpu lost", lost_since=lost_since)
                log.warning("gpuguard: rebooting to bring the GPU back")
                await asyncio.to_thread(subprocess.run, ["sudo", "-n", "/usr/bin/systemctl", "reboot"],
                                        capture_output=True, timeout=60)
        try:
            await asyncio.wait_for(stop.wait(), CHECK_EVERY)
        except asyncio.TimeoutError:
            pass

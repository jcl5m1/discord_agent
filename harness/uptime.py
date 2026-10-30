"""Network and system uptime log, plus the statistics behind /uptime.

Runs as its own systemd user unit (discord-agent-uptime.service), independent
of the bot, so it keeps logging while the harness is down or restarting:

    python -m harness.uptime

Every UPTIME_INTERVAL seconds it checks internet reachability (TCP connects to
a few well-known anycast IPs; up if any answers) and rewrites a tiny state file
with a "last alive" timestamp. Only *state changes* are appended to the log
(data/uptime.jsonl), one JSON object per line:

    monitor_start   monitor came up             {boot_id, boot}
    monitor_stop    monitor stopped cleanly     {reason: "shutdown" | "stop"}
    system_down     machine was off/rebooting   {start, end, clean, source}
    monitor_gap     same boot, monitor not running (suspend, stopped unit) {start, end}
    net_down        internet unreachable since ts   {targets}
    net_up          internet back               {down_since}

A reboot is detected on startup by a changed kernel boot id: the system was
down from the previous run's last-alive time (or its clean-stop time) to this
boot. On the very first run, earlier reboots are backfilled from the journal.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import time
from pathlib import Path

from . import config

log = logging.getLogger("uptime")

LOG_PATH: Path = config.DATA_DIR / "uptime.jsonl"
STATE_PATH: Path = config.DATA_DIR / "uptime_state.json"
INTERVAL: float = config.UPTIME_INTERVAL
TARGETS: list[tuple[str, int]] = config.UPTIME_TARGETS
CONNECT_TIMEOUT = 3.0
DOWN_AFTER = 2  # consecutive failed checks before an outage is declared
# A longer silence than this within one boot means the monitor wasn't running.
GAP_AFTER = INTERVAL * 3


# --- storage ---

def _append(event: str, ts: float | None = None, **fields) -> dict:
    rec = {"ts": round(ts if ts is not None else time.time(), 3), "event": event, **fields}
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a") as f:
        f.write(json.dumps(rec) + "\n")
        f.flush()
        os.fsync(f.fileno())
    log.info("%s %s", event, {k: v for k, v in rec.items() if k not in ("ts", "event")})
    return rec


def read_events() -> list[dict]:
    if not LOG_PATH.exists():
        return []
    events = []
    for line in LOG_PATH.read_text().splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a torn last line after a power cut
    return events


def read_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _write_state(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(STATE_PATH)


def _boot() -> tuple[str, float]:
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip().replace("-", "")
    for line in Path("/proc/stat").read_text().splitlines():
        if line.startswith("btime "):
            return boot_id, float(line.split()[1])
    return boot_id, time.time()


def _system_stopping() -> bool:
    """True while the machine is shutting down or rebooting."""
    try:
        out = subprocess.run(
            ["systemctl", "is-system-running"], capture_output=True, text=True, timeout=5
        ).stdout.strip()
    except Exception:
        return False
    return out == "stopping"


def _backfill_from_journal(before: float) -> None:
    """First run only: record earlier reboots from the journal's boot list."""
    try:
        out = subprocess.run(
            ["journalctl", "--list-boots", "--no-pager", "-o", "json"],
            capture_output=True, text=True, timeout=30,
        ).stdout
        boots = sorted(json.loads(out), key=lambda b: b["index"])
    except Exception:
        log.warning("journal boot history unavailable; no backfill")
        return
    for prev, nxt in zip(boots, boots[1:]):
        start, end = prev["last_entry"] / 1e6, nxt["first_entry"] / 1e6
        if 0 < end - start and end <= before:  # skip clock-skewed boots
            _append("system_down", ts=end, start=start, end=end, clean=None, source="journal")


# --- monitor ---

async def _reachable(host: str, port: int) -> bool:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), CONNECT_TIMEOUT)
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    return True


async def check_network() -> bool:
    results = await asyncio.gather(*(_reachable(h, p) for h, p in TARGETS))
    return any(results)


def _on_start(state: dict, boot_id: str, boot: float, now: float) -> None:
    """Log what happened while the monitor wasn't running."""
    if not LOG_PATH.exists():
        _backfill_from_journal(before=boot)
    last = state.get("stopped_at") or state.get("last_alive")
    if last and state.get("boot_id") != boot_id:
        if boot > last:
            _append("system_down", ts=boot, start=last, end=boot,
                    clean=state.get("stop_reason") == "shutdown", source="monitor")
        else:  # clock was wrong on one side of the reboot; the span is unknowable
            log.warning("reboot detected but boot time precedes last-alive; not logged")
    elif last and now - last > GAP_AFTER:
        _append("monitor_gap", ts=now, start=last, end=now)
    _append("monitor_start", ts=now, boot_id=boot_id, boot=boot)


async def run() -> None:
    boot_id, boot = _boot()
    now = time.time()
    _on_start(read_state(), boot_id, boot, now)

    net_up = True  # assume up until DOWN_AFTER consecutive failures say otherwise
    net_since = now
    first_fail: float | None = None
    fails = 0
    last_alive = now
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    while not stop.is_set():
        t = time.time()
        if t - last_alive > GAP_AFTER:  # suspended, or the process was frozen
            _append("monitor_gap", ts=t, start=last_alive, end=t)
            fails, first_fail = 0, None
        ok = await check_network()
        if ok:
            fails, first_fail = 0, None
            if not net_up:
                _append("net_up", ts=t, down_since=net_since)
                net_up, net_since = True, t
        else:
            fails += 1
            first_fail = first_fail or t
            if net_up and fails >= DOWN_AFTER:
                _append("net_down", ts=first_fail, targets=[h for h, _ in TARGETS])
                net_up, net_since = False, first_fail
        last_alive = time.time()
        _write_state({
            "boot_id": boot_id, "boot": boot, "last_alive": last_alive,
            "net": "up" if net_up else "down", "net_since": net_since,
        })
        try:
            await asyncio.wait_for(stop.wait(), INTERVAL)
        except asyncio.TimeoutError:
            pass

    now = time.time()
    reason = "shutdown" if _system_stopping() else "stop"
    _append("monitor_stop", ts=now, reason=reason)
    _write_state({
        "boot_id": boot_id, "boot": boot, "last_alive": now, "stopped_at": now,
        "stop_reason": reason, "net": "up" if net_up else "down", "net_since": net_since,
    })


# --- statistics ---

def _intervals(events: list[dict], now: float) -> tuple[list, list, list, float | None]:
    """Rebuild (system_down, net_down, unmonitored) intervals and coverage start."""
    sys_down, net_down, gaps = [], [], []
    open_net: float | None = None
    covered_from: float | None = None

    def close_net(at: float) -> None:
        nonlocal open_net
        if open_net is not None:
            net_down.append((open_net, max(open_net, at)))
            open_net = None

    for e in sorted(events, key=lambda e: e["ts"]):
        kind = e["event"]
        if kind == "system_down":
            close_net(e["start"])
            sys_down.append((e["start"], e["end"], e.get("clean")))
        elif kind == "monitor_gap":
            close_net(e["start"])
            gaps.append((e["start"], e["end"]))
        elif kind == "monitor_start":
            close_net(e["ts"])
            covered_from = covered_from or e["ts"]
        elif kind == "monitor_stop":
            close_net(e["ts"])
        elif kind == "net_down":
            open_net = e["ts"]
        elif kind == "net_up":
            close_net(e["ts"])
    close_net(now)
    return sys_down, net_down, gaps, covered_from


def _overlap(intervals, lo: float, hi: float) -> tuple[float, int]:
    total, count = 0.0, 0
    for iv in intervals:
        a, b = max(iv[0], lo), min(iv[1], hi)
        if b > a:
            total += b - a
            count += 1
    return total, count


def fmt_duration(seconds: float) -> str:
    s = int(round(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    if s < 86400:
        return f"{s // 3600}h {(s % 3600) // 60:02d}m"
    return f"{s // 86400}d {(s % 86400) // 3600}h"


def summary(now: float | None = None, recent: int = 5) -> str:
    """Discord-markdown report for /uptime."""
    now = now or time.time()
    events = read_events()
    if not events:
        return "No uptime data yet — is `discord-agent-uptime.service` running?"
    state = read_state()
    sys_down, net_down, gaps, covered_from = _intervals(events, now)
    sys_from = min([s for s, _, _ in sys_down] + [covered_from or now])

    lines = ["**Uptime**"]
    boot = state.get("boot")
    if boot:
        lines.append(f"🖥️ System up since <t:{int(boot)}:f> ({fmt_duration(now - boot)})")
    alive = state.get("last_alive", 0)
    if now - alive > GAP_AFTER and not state.get("stopped_at"):
        lines.append(f"⚠️ monitor silent since <t:{int(alive)}:R> — check `discord-agent-uptime.service`")
    elif state.get("stopped_at"):
        lines.append(f"⚠️ monitor stopped <t:{int(state['stopped_at'])}:R>")
    elif state.get("net") == "down":
        lines.append(f"🔴 Internet **DOWN** since <t:{int(state['net_since'])}:T> "
                     f"({fmt_duration(now - state['net_since'])})")
    else:
        # From the log, not the state file: the monitor restarting isn't an outage.
        since = max([b for _, b in net_down] + [b for _, b, _ in sys_down] + [covered_from or now])
        lines.append(f"🟢 Internet up since <t:{int(since)}:f> ({fmt_duration(now - since)})"
                     + ("" if net_down else " — no outages recorded yet"))

    lines += ["", "```", f"{'window':<8}{'internet':>26}{'system':>26}",
              f"{'':<8}{'avail · outages · down':>26}{'avail · outages · down':>26}"]
    for label, span in (("24h", 86400), ("7d", 7 * 86400), ("30d", 30 * 86400), ("all", None)):
        lo_net = max(now - span, covered_from or now) if span else (covered_from or now)
        lo_sys = max(now - span, sys_from) if span else sys_from
        cells = []
        for intervals, lo, excluded in (
            (net_down, lo_net, sys_down + gaps),  # net only counts while monitored
            ([(a, b) for a, b, _ in sys_down], lo_sys, []),
        ):
            window = now - lo
            excl, _ = _overlap(excluded, lo, now)
            window -= excl
            down, n = _overlap(intervals, lo, now)
            if window <= 0:
                cells.append("—")
                continue
            pct = 100 * (1 - down / window)
            cells.append(f"{pct:.2f}% {n}× {fmt_duration(down)}" if n else f"{pct:.2f}%")
        lines.append(f"{label:<8}{cells[0]:>26}{cells[1]:>26}")
    lines.append("```")
    lines.append(f"_internet tracked since <t:{int(covered_from or now)}:d>, "
                 f"system since <t:{int(sys_from)}:d>; checks every {int(INTERVAL)}s_")

    outages = sorted(
        [("🌐 internet", a, b) for a, b in net_down]
        + [("🔌 system " + ("(clean shutdown)" if c else "(crash/power)" if c is False else "down"), a, b)
           for a, b, c in sys_down],
        key=lambda o: o[1], reverse=True,
    )[:recent]
    if outages:
        lines += ["", "**Recent outages**"]
        for kind, a, b in outages:
            ongoing = " (ongoing)" if b >= now - 1 else ""
            lines.append(f"• {kind} <t:{int(a)}:f> — {fmt_duration(b - a)}{ongoing}")
    return "\n".join(lines)


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(run())


if __name__ == "__main__":
    main()

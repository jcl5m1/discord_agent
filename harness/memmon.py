"""Process recorder: which processes used the memory and the CPU (health telemetry).

Runs inside the uptime monitor (python -m harness.uptime). Linux only.

- Each minute it appends one line to data/mem/<month>.csv: available memory,
  swap use, memory pressure (PSI: the share of time tasks waited for memory),
  the TOP_N processes by memory (resident + swapped) and the TOP_N by CPU since
  the previous line, each with its command line. When available memory is
  below LOW_PCT, it writes a line every TIGHT_EVERY seconds, so the minutes
  before an out-of-memory freeze are recorded in detail. Lines are synced to
  disk, so they survive a hard reset.
- Events in data/uptime.jsonl (shown by /uptime): `mem_low` when available
  memory falls below LOW_PCT (with the top processes), `mem_ok` when it is back
  above OK_PCT.
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Callable

from . import config

log = logging.getLogger("memmon")

DIR: Path = config.DATA_DIR / "mem"
EVERY = 60.0
TIGHT_EVERY = 10.0
LOW_PCT = 10.0  # available memory below this % of RAM: record every TIGHT_EVERY s, and a mem_low event
OK_PCT = 15.0  # back above this: a mem_ok event
TOP_N = 6
HEADER = ["time", "avail_pct", "avail_mib", "swap_used_pct", "psi_some_avg10", "psi_full_avg10",
          "top_mem", "top_cpu"]
TICK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


def _meminfo() -> dict[str, int]:
    out = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, _, rest = line.partition(":")
        out[key] = int(rest.split()[0])  # kB
    return out


def _psi() -> tuple[float | None, float | None]:
    try:
        lines = Path("/proc/pressure/memory").read_text().splitlines()
    except OSError:
        return None, None
    vals = [float(dict(x.split("=") for x in line.split()[1:]).get("avg10", 0)) for line in lines[:2]]
    vals += [None, None]
    return vals[0], vals[1]


def _cmd(d: Path) -> str:
    try:
        cmd = (d / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
    except OSError:
        return ""
    if not cmd:  # a kernel thread
        try:
            cmd = f"[{(d / 'comm').read_text().strip()}]"
        except OSError:
            pass
    return cmd[:200]


class Recorder:
    def __init__(self, append_event: Callable[..., dict] | None = None):
        self.append_event = append_event
        self.prev_cpu: dict[int, int] = {}  # pid -> CPU ticks at the previous sample
        self.prev_t = 0.0
        self.low = False

    def processes(self) -> tuple[list[dict], list[dict]]:
        """(top by memory, top by CPU % since the previous call)."""
        now = time.monotonic()
        mem, cpu, ticks = [], [], {}
        for d in Path("/proc").iterdir():
            if not d.name.isdigit():
                continue
            pid = int(d.name)
            try:
                stat = (d / "stat").read_text()
                fields = stat[stat.rindex(")") + 2:].split()
                ticks[pid] = int(fields[11]) + int(fields[12])  # utime + stime
                status = {k: v for k, _, v in (x.partition(":") for x in (d / "status").read_text().splitlines())}
                rss = int(status.get("VmRSS", "0 kB").split()[0])
                swap = int(status.get("VmSwap", "0 kB").split()[0])
            except (OSError, ValueError, IndexError):
                continue
            if rss + swap:
                mem.append((rss + swap, rss, swap, d))
            if self.prev_t and pid in self.prev_cpu:
                pct = 100 * (ticks[pid] - self.prev_cpu[pid]) / TICK / (now - self.prev_t)
                if pct >= 1:
                    cpu.append((pct, d))
        self.prev_cpu, self.prev_t = ticks, now
        top_mem = [{"pid": int(d.name), "rss_mib": rss // 1024, "swap_mib": swap // 1024, "cmd": _cmd(d)}
                   for _, rss, swap, d in sorted(mem, key=lambda p: -p[0])[:TOP_N]]
        top_cpu = [{"pid": int(d.name), "cpu_pct": round(pct), "cmd": _cmd(d)}
                   for pct, d in sorted(cpu, key=lambda p: -p[0])[:TOP_N]]
        return top_mem, top_cpu

    def sample(self) -> list:
        m = _meminfo()
        total, avail = m["MemTotal"], m["MemAvailable"]
        swap_total, swap_free = m.get("SwapTotal", 0), m.get("SwapFree", 0)
        some, full = _psi()
        top_mem, top_cpu = self.processes()
        return [time.strftime("%Y-%m-%d %H:%M:%S"), f"{100 * avail / total:.1f}", avail // 1024,
                f"{100 * (swap_total - swap_free) / swap_total:.1f}" if swap_total else "",
                "" if some is None else some, "" if full is None else full,
                json.dumps(top_mem, separators=(",", ":")), json.dumps(top_cpu, separators=(",", ":"))]

    def events(self, row: list) -> None:
        avail = float(row[1])
        if not self.append_event:
            return
        if not self.low and avail < LOW_PCT:
            self.low = True
            top = [f"{p['rss_mib'] + p['swap_mib']} MiB {p['cmd'][:80]}" for p in json.loads(row[6])[:3]]
            self.append_event("mem_low", avail_pct=avail, swap_used_pct=row[3], top=top)
        elif self.low and avail > OK_PCT:
            self.low = False
            self.append_event("mem_ok", avail_pct=avail)


def write(row: list) -> None:
    path = DIR / time.strftime("%Y-%m.csv")
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(HEADER)
        w.writerow(row)
        f.flush()
        os.fsync(f.fileno())


def latest() -> dict | None:
    """The newest recorded line, as a dict (None if there is none)."""
    files = sorted(DIR.glob("*.csv"))
    if not files:
        return None
    with files[-1].open(newline="") as f:
        rows = list(csv.reader(f))
    if len(rows) < 2 or rows[0] != HEADER:
        return None
    return dict(zip(HEADER, rows[-1]))


def _quick() -> tuple[float, float | None, float | None, float | None]:
    """(available %, swap used %, PSI some avg10, PSI full avg10): cheap, for every tick."""
    m = _meminfo()
    swap = (100 * (m["SwapTotal"] - m["SwapFree"]) / m["SwapTotal"]) if m.get("SwapTotal") else None
    return (100 * m["MemAvailable"] / m["MemTotal"], swap, *_psi())


def _bot_pid() -> set[int]:
    try:
        return {int(json.loads(config.HEARTBEAT_PATH.read_text())["pid"])}
    except (OSError, ValueError, KeyError, TypeError):
        return set()


async def run(stop: asyncio.Event, append_event: Callable[..., dict] | None = None) -> None:
    if not sys.platform.startswith("linux") or not config.MEM_MONITOR:
        return
    from . import health

    log.info("memmon: recording to %s; health state in %s", DIR, health.STATE_PATH)
    rec = Recorder(append_event)
    actor = health.Actor(append_event)
    await asyncio.to_thread(rec.processes)  # a CPU baseline for the first line
    last_line, level, actions = 0.0, "ok", []
    top_mem: list = []
    top_cpu: list = []
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), TIGHT_EVERY if level == "ok" else 2.0)
        except asyncio.TimeoutError:
            pass
        if stop.is_set():
            break
        try:
            avail, swap, some, full = await asyncio.to_thread(_quick)
            level, reasons = health.classify(avail, swap, some, full)
            now = time.time()
            due = now - last_line >= (EVERY if avail >= LOW_PCT else TIGHT_EVERY)
            if due or level == "critical":
                row = await asyncio.to_thread(rec.sample)
                top_mem, top_cpu = json.loads(row[6]), json.loads(row[7])
                await asyncio.to_thread(write, row)
                rec.events(row)
                last_line = now
            if level == "critical" and config.HEALTH_ACTIONS:
                done = await asyncio.to_thread(actor.act, top_mem, _bot_pid())
                if done:
                    actions = (actions + [{"ts": now, "text": done}])[-10:]
            health.write_state({"ts": now, "level": level, "reasons": reasons, "avail_pct": round(avail, 1),
                                "swap_used_pct": None if swap is None else round(swap, 1),
                                "psi_some": some, "psi_full": full, "top_mem": top_mem[:5],
                                "top_cpu": top_cpu[:5], "actions": actions})
        except Exception:
            log.exception("memmon sample failed")


def summary_lines(events: list[dict], now: float, days: int = 30) -> list[str]:
    """Lines for /uptime: memory now, the top processes, and low-memory events."""
    lines = []
    cur = latest()
    if cur:
        lines += ["", f"**Memory** ({cur['time'][11:16]}): {cur['avail_pct']}% available, "
                      f"swap {cur['swap_used_pct'] or '?'}% used, pressure {cur['psi_some_avg10'] or '?'}%"]
        for p in json.loads(cur["top_mem"])[:3]:
            lines.append(f"• {(p['rss_mib'] + p['swap_mib']) / 1024:.1f} GB — `{_short(p['cmd'])}`")
        busy = json.loads(cur["top_cpu"])[:3]
        if busy:
            lines.append("**CPU**: " + " · ".join(f"{p['cpu_pct']}% `{_short(p['cmd'], 40)}`" for p in busy))
    low = [e for e in events if e.get("event") == "mem_low" and now - e["ts"] < days * 86400]
    if low:
        lines.append(f"**Low memory** (last {days} days): {len(low)}")
        for e in low[-3:]:
            top = (e.get("top") or ["?"])[0]
            lines.append(f"• <t:{int(e['ts'])}:f> — {e.get('avail_pct')}% free; top: `{_short(top, 60)}`")
    return lines


def _short(cmd: str, n: int = 70) -> str:
    cmd = cmd.replace("`", "'")
    return cmd if len(cmd) <= n else cmd[: n - 1] + "…"

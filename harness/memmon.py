"""Memory recorder: which processes used the memory, before a machine runs out.

Runs inside the uptime monitor (python -m harness.uptime). Linux only.

Each minute it appends one line to data/mem/<month>.csv: available memory,
swap use, memory pressure (PSI: the share of time tasks waited for memory), and
the TOP_N processes by resident memory, with their command lines. When
available memory is below TIGHT_PCT, it writes a line every TIGHT_EVERY seconds
instead, so the minutes before an out-of-memory freeze are recorded in detail.
Each line is flushed and synced, so it survives a hard reset.
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

from . import config

log = logging.getLogger("memmon")

DIR: Path = config.DATA_DIR / "mem"
EVERY = 60.0
TIGHT_EVERY = 10.0
TIGHT_PCT = 10.0  # available memory below this % of RAM: record every TIGHT_EVERY seconds
TOP_N = 6
HEADER = ["time", "avail_pct", "avail_mib", "swap_used_pct", "psi_some_avg10", "psi_full_avg10", "top"]


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
    vals = []
    for line in lines[:2]:
        part = dict(x.split("=") for x in line.split()[1:])
        vals.append(float(part.get("avg10", 0)))
    return (vals + [None, None])[0], (vals + [None, None])[1]


def top_processes(n: int = TOP_N) -> list[dict]:
    """The n processes with the most resident memory: pid, rss MiB, swap MiB, command line."""
    procs = []
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            status = {k: v for k, _, v in (x.partition(":") for x in (d / "status").read_text().splitlines())}
            rss = int(status.get("VmRSS", "0 kB").split()[0])
            swap = int(status.get("VmSwap", "0 kB").split()[0])
            if rss + swap == 0:
                continue
            procs.append((rss, swap, d))
        except (OSError, ValueError):
            continue
    out = []
    for rss, swap, d in sorted(procs, key=lambda p: -(p[0] + p[1]))[:n]:
        try:
            cmd = (d / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
        except OSError:
            cmd = ""
        out.append({"pid": int(d.name), "rss_mib": rss // 1024, "swap_mib": swap // 1024, "cmd": cmd[:200]})
    return out


def sample() -> list:
    m = _meminfo()
    total, avail = m["MemTotal"], m["MemAvailable"]
    swap_total, swap_free = m.get("SwapTotal", 0), m.get("SwapFree", 0)
    some, full = _psi()
    return [time.strftime("%Y-%m-%d %H:%M:%S"), f"{100 * avail / total:.1f}", avail // 1024,
            f"{100 * (swap_total - swap_free) / swap_total:.1f}" if swap_total else "",
            "" if some is None else some, "" if full is None else full,
            json.dumps(top_processes(), separators=(",", ":"))]


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


async def run(stop: asyncio.Event) -> None:
    if not sys.platform.startswith("linux") or not config.MEM_MONITOR:
        return
    log.info("memmon: recording to %s", DIR)
    last = 0.0
    while not stop.is_set():
        try:
            row = await asyncio.to_thread(sample)
            tight = float(row[1]) < TIGHT_PCT
            if tight or time.time() - last >= EVERY:
                await asyncio.to_thread(write, row)
                last = time.time()
        except Exception:
            log.exception("memmon sample failed")
            tight = False
        try:
            await asyncio.wait_for(stop.wait(), TIGHT_EVERY)
        except asyncio.TimeoutError:
            pass

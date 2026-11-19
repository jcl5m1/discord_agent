"""Disk space: levels for the watchdog's alerts and the bot's "disk full" notice.

A full main disk stops everything quietly: the agents can't write, the bot
can't log or save its heartbeat, and the uptime monitor can't record. So the
watchdog checks the disks every minute and alerts in Discord when one rises to
`warn` (DISK_WARN_PCT used) or `critical` (DISK_CRIT_PCT), and at `full`
(under DISK_FULL_MB free). It says so again when the disk is back under
DISK_OK_PCT.
"""

from __future__ import annotations

import errno
import os
import shutil
from pathlib import Path

from . import config

LEVELS = ("ok", "warn", "critical", "full")


def paths() -> list[Path]:
    """The disks to watch: the root disk and the one holding the harness data, once each."""
    seen, out = set(), []
    for p in (Path("/"), config.DATA_DIR, *config.DISK_PATHS):
        try:
            dev = os.stat(p).st_dev
        except OSError:
            continue
        if dev not in seen:
            seen.add(dev)
            out.append(Path(p))
    return out


def usage(path: Path) -> tuple[float, int]:
    """(% used, bytes free) as `df` shows it."""
    u = shutil.disk_usage(path)
    return 100 * u.used / (u.used + u.free) if u.used + u.free else 0.0, u.free


def level(pct: float, free: int, prev: str = "ok") -> str:
    if free < config.DISK_FULL_MB * 1024 * 1024:
        return "full"
    if pct >= config.DISK_CRIT_PCT:
        return "critical"
    if pct >= config.DISK_WARN_PCT:
        return "warn"
    if prev != "ok" and pct >= config.DISK_OK_PCT:
        return "warn"  # don't flap: stay at warn until clearly below
    return "ok"


def is_full_error(exc: BaseException) -> bool:
    return isinstance(exc, OSError) and exc.errno in (errno.ENOSPC, errno.EDQUOT)


def fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def biggest_dirs(path: Path, n: int = 5, timeout: float = 45) -> list[tuple[int, str]]:
    """The largest folders directly under the user's home on this disk (`du`, best effort, time-limited)."""
    import subprocess
    home = Path.home()
    try:
        if os.stat(home).st_dev != os.stat(path).st_dev:
            return []
        r = subprocess.run(["du", "-x", "-d", "1", "-B1", str(home)], capture_output=True, text=True,
                           timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return []
    rows = []
    for line in r.stdout.splitlines():
        size, _, name = line.partition("\t")
        if name and name != str(home) and size.isdigit():
            rows.append((int(size), name))
    return sorted(rows, reverse=True)[:n]

"""System health: memory levels, the state file, and the last-resort action.

Memory is the cause of the hard crashes so far: programs fill the RAM and the
swap, the machine thrashes, and it freezes. Three parts work together:

- The process recorder (memmon, in the uptime monitor) samples memory every 10 s
  (every 2 s when not ok), classifies it here, and writes data/health.json.
  At `critical` it acts at once (act()): it stops the biggest process that is
  safe to stop, because an agent would be too slow then.
- The watchdog posts a Discord alert when the level rises.
- The bot tells the health agent (HEALTH_AGENT, default: the instance that works
  in the harness repo) to take preventive measures at `warn`, before it is
  critical: find what grows, ask its owner to pause it, stop or restart jobs,
  and add memory limits.

Levels (available memory = MemAvailable, % of RAM; PSI = /proc/pressure/memory):
  warn      available < HEALTH_WARN_PCT, or swap >= 80% used with available < 30%,
            or PSI "some" avg10 >= 20%
  critical  available < HEALTH_CRIT_PCT, or PSI "full" avg10 >= 30%
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

from . import config

log = logging.getLogger("health")

STATE_PATH: Path = config.DATA_DIR / "health.json"
LEVELS = ("ok", "warn", "critical")
MIN_TARGET_MIB = 1024  # never act on a process smaller than this
KILL_AFTER = 15.0  # seconds after SIGTERM before SIGKILL, if memory is still critical
# Never stopped by the harness: the agents, the desktop, remote access, databases, container daemons.
PROTECTED_NAMES = {
    "claude", "kimi", "muse", "node", "systemd", "sshd", "sshd-session", "dbus-daemon", "dbus-broker",
    "Xorg", "Xwayland", "gnome-shell", "gdm", "pipewire", "wireplumber", "pulseaudio", "postgres",
    "dockerd", "containerd", "containerd-shim", "containerd-shim-runc-v2", "tailscaled", "cloudflared",
    "earlyoom", "journald", "systemd-journald", "NetworkManager", "polkitd",
}
PROTECTED_CMD = re.compile(r"-m harness\.(bot|uptime|watchdog)\b|harness/(bot|uptime|watchdog)\.py")
CONTAINER = re.compile(r"docker[-/]([0-9a-f]{64})")


def classify(avail_pct: float, swap_pct: float | None, psi_some: float | None,
             psi_full: float | None) -> tuple[str, list[str]]:
    """(level, reasons) for one memory sample."""
    crit, warn = [], []
    if avail_pct < config.HEALTH_CRIT_PCT:
        crit.append(f"only {avail_pct:.1f}% of RAM available")
    if psi_full is not None and psi_full >= 30:
        crit.append(f"all tasks stalled on memory {psi_full:.0f}% of the time")
    if crit:
        return "critical", crit
    if avail_pct < config.HEALTH_WARN_PCT:
        warn.append(f"{avail_pct:.1f}% of RAM available")
    if swap_pct is not None and swap_pct >= 80 and avail_pct < 30:
        warn.append(f"swap {swap_pct:.0f}% used, {avail_pct:.1f}% of RAM available")
    if psi_some is not None and psi_some >= 20:
        warn.append(f"tasks waited for memory {psi_some:.0f}% of the time")
    return ("warn", warn) if warn else ("ok", [])


def write_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(STATE_PATH)


def read_state() -> dict | None:
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        return None


def _name(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        return ""


def _uid(pid: int) -> int | None:
    try:
        return Path(f"/proc/{pid}").stat().st_uid
    except OSError:
        return None


def container_of(pid: int) -> str | None:
    """The Docker container id of a process, if it runs in one."""
    try:
        m = CONTAINER.search(Path(f"/proc/{pid}/cgroup").read_text())
    except OSError:
        return None
    return m.group(1) if m else None


def protected(pid: int, cmd: str, extra: set[int] = frozenset()) -> bool:
    return (pid in extra or pid == os.getpid() or _name(pid) in PROTECTED_NAMES
            or bool(PROTECTED_CMD.search(cmd)))


def pick_target(top_mem: list[dict], extra_protected: set[int] = frozenset()) -> dict | None:
    """The biggest process that is safe to stop (at least MIN_TARGET_MIB)."""
    for p in top_mem:
        size = p.get("rss_mib", 0) + p.get("swap_mib", 0)
        if size < MIN_TARGET_MIB:
            return None
        if not protected(p["pid"], p.get("cmd", ""), extra_protected):
            return {**p, "size_mib": size}
    return None


class Actor:
    """The last-resort action at `critical`: stop the biggest process that is safe to stop."""

    def __init__(self, append_event=None):
        self.append_event = append_event
        self.pending: dict[int, float] = {}  # pid -> when SIGTERM was sent
        self.last_action = 0.0

    def act(self, top_mem: list[dict], extra_protected: set[int] = frozenset()) -> str | None:
        """Called at each critical sample. Returns a line about what was done, or None."""
        now = time.time()
        for pid, sent in list(self.pending.items()):  # escalate: still there after KILL_AFTER
            if not Path(f"/proc/{pid}").exists():
                self.pending.pop(pid)
            elif now - sent >= KILL_AFTER:
                self.pending.pop(pid)
                return self._record("kill", pid, "", 0, self._signal(pid, signal.SIGKILL))
        if self.pending or now - self.last_action < KILL_AFTER:
            return None
        target = pick_target(top_mem, extra_protected)
        if target is None:
            return None
        self.last_action = now
        pid, cmd, size = target["pid"], target.get("cmd", ""), target["size_mib"]
        if _uid(pid) == os.getuid():
            result = self._signal(pid, signal.SIGTERM)
            if result == "ok":
                self.pending[pid] = now
            return self._record("stop", pid, cmd, size, result)
        cid = container_of(pid)
        if cid and shutil.which("docker"):
            try:
                r = subprocess.run(["docker", "restart", "-t", "10", cid[:12]],
                                   capture_output=True, text=True, timeout=60)
                result = "ok" if r.returncode == 0 else (r.stderr.strip() or f"rc {r.returncode}")[:200]
            except (OSError, subprocess.TimeoutExpired) as e:
                result = str(e)[:200]
            return self._record("restart container", pid, cmd, size, result, container=cid[:12])
        return self._record("none", pid, cmd, size, "not ours to stop (another user); earlyoom or the kernel must")

    @staticmethod
    def _signal(pid: int, sig: int) -> str:
        try:
            os.kill(pid, sig)
            return "ok"
        except OSError as e:
            return str(e)

    def _record(self, action: str, pid: int, cmd: str, size: int, result: str, **extra) -> str:
        line = (f"{action} pid {pid} ({size} MiB) {cmd[:120]}: {result}" if action != "kill"
                else f"kill pid {pid} (still there {KILL_AFTER:.0f}s after the stop request): {result}")
        log.warning("health: %s", line)
        if self.append_event:
            self.append_event("health_action", action=action, pid=pid, size_mib=size, cmd=cmd[:200],
                              result=result, **extra)
        return line

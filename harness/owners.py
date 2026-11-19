"""Who owns a process: its systemd unit, cron, a Docker container, a heavy job, or a harness agent.

The health monitor uses this, so a warning names the owner of each top process,
and the health agent can message that owner at once instead of tracing parent
processes and cgroups by hand.

    ctx = Context()          # once per sample: container names, agent sessions, heavy jobs
    owner(pid, ctx)          # e.g. "agent placeframe-compute", "unit pf-frames-rebuild (MemoryMax 8G)",
                             #      "container placeframe-localizer-cuda-1", "cron", "heavy video-decode job of harness-2@m"
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from . import config

_SESSION = re.compile(r"(?:--resume|--session-id|--session)[ =]([0-9a-f-]{8,})")
_DOCKER = re.compile(r"docker-([0-9a-f]{64})\.scope")
_UNIT = re.compile(r"/([^/]+)\.(service|scope)$")
_AGENT_CLIS = {"claude", "kimi", "muse"}


def _gib(text: str) -> str:
    try:
        n = int(text)
    except ValueError:
        return text  # "max"
    return f"{n / 1024 ** 3:.0f}G" if n >= 1024 ** 3 else f"{n // 1024 ** 2}M"


class Context:
    """What one sample needs to name owners: container names, agent sessions, heavy jobs, parents."""

    def __init__(self) -> None:
        self.containers: dict[str, str] = {}
        if shutil.which("docker"):
            try:
                out = subprocess.run(["docker", "ps", "--no-trunc", "--format", "{{.ID}} {{.Names}}"],
                                     capture_output=True, text=True, timeout=10).stdout
                self.containers = dict(line.split(" ", 1) for line in out.splitlines() if " " in line)
            except (OSError, subprocess.TimeoutExpired):
                pass
        self.sessions: dict[str, str] = {}  # agent session id -> instance name
        try:
            for inst in json.loads((config.DATA_DIR / "instances.json").read_text()).values():
                if inst.get("session_id"):
                    self.sessions[inst["session_id"]] = inst.get("name", "?")
        except (OSError, ValueError, AttributeError):
            pass
        self.heavy: dict[str, str] = {}  # heavy unit -> who
        try:
            from . import heavy
            self.heavy = {h.get("unit"): h.get("who", "?") for h in heavy.holders()}
        except Exception:
            pass
        self._agent: dict[int, str | None] = {}


def _cgroup(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cgroup").read_text().split(":", 2)[2].strip()
    except (OSError, IndexError):
        return ""


def _ppid(pid: int) -> int:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return int(stat[stat.rindex(")") + 2:].split()[1])
    except (OSError, ValueError, IndexError):
        return 0


def _agent_of(pid: int, ctx: Context) -> str | None:
    """The harness agent whose process tree holds pid (its CLI's session id names the instance)."""
    chain, p = [], pid
    while p > 1 and len(chain) < 64:
        if p in ctx._agent:
            name = ctx._agent[p]
            break
        chain.append(p)
        name = None
        try:
            comm = Path(f"/proc/{p}/comm").read_text().strip()
            if comm in _AGENT_CLIS:
                cmd = Path(f"/proc/{p}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
                m = _SESSION.search(cmd)
                name = ctx.sessions.get(m.group(1), f"{comm} session {m.group(1)[:8]}") if m else comm
                break
        except OSError:
            break
        p = _ppid(p)
    for q in chain:
        ctx._agent[q] = name
    return name


def owner(pid: int, ctx: Context) -> str:
    cg = _cgroup(pid)
    m = _DOCKER.search(cg)
    if m:
        return f"container {ctx.containers.get(m.group(1), m.group(1)[:12])}"
    if cg.endswith("/cron.service"):
        return "cron"
    m = _UNIT.search(cg)
    unit = m.group(1) if m else ""
    if unit.startswith("heavy-"):
        cls = unit[len("heavy-"):].rsplit("-", 1)[0] or "?"  # heavy-<class>-<pid>; a class may have hyphens
        return f"heavy {cls} job of {ctx.heavy.get(unit, '?')}"
    agent = _agent_of(pid, ctx)
    if unit and unit != "discord-agent" and not unit.startswith(("session-", "user@", "init")):
        limit = ""
        try:
            limit = _gib(Path(f"/sys/fs/cgroup{cg}/memory.max").read_text().strip())
        except OSError:
            pass
        text = f"unit {unit}" + (f" (MemoryMax {limit})" if limit and limit != "max" else "")
        return f"{text}, started by agent {agent}" if agent else text
    if agent:
        return f"agent {agent}"
    if unit.startswith("session-"):
        return "a terminal or SSH session"
    return unit or "?"

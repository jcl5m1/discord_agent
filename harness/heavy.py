"""heavy: run a heavy job with a memory limit, in a machine-wide slot, when the machine has room.

Every agent on the machine shares it, so jobs stop colliding:

    heavy --class video-decode --mem 8G -- ffmpeg -i in.insv ...
    heavy --class ml-cpu --mem 6G --cpus 4 -- python person_masks.py detect /data
    heavy status

- Slots: each class has HEAVY_SLOTS slots on the machine (default video-decode=1, gpu=1, ml-cpu=2,
  default=3). A job waits for a free slot of its class and says who holds them.
- Health gate: a job waits while system health (memory) is warn or critical, or the disk is full
  (data/health.json, data/disk.json). --force skips the gate, not the slot.
- Limits: the job runs in its own systemd scope (systemd-run --user --scope) with MemoryMax (--mem),
  MemorySwapMax (--swap) and, with --cpus, CPUQuota. Over the limit, the kernel stops the job, not the
  machine. The scope is named heavy-<class>-<pid>; HEAVY_UNIT holds the name.
- Stops with its caller: if heavy is stopped (Ctrl-C, SIGTERM, a timeout) it stops the whole scope and
  the Docker containers labelled heavy.unit=$HEAVY_UNIT (start them with `--label heavy.unit=$HEAVY_UNIT`
  and a `--memory` limit: containers run outside the scope). If heavy itself is killed hard, the next
  heavy run or the uptime monitor's sweep stops what it left.
- Log: one line per job in data/heavy/jobs.jsonl (who, class, command, wait, peak memory, exit code).

Exit code: the job's; 75 if it waited longer than --max-wait; 2 for a usage error.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import config

DIR: Path = config.DATA_DIR / "heavy"
LOG: Path = DIR / "jobs.jsonl"
POLL = 5.0
GATE_FRESH = 300  # health/disk state older than this is ignored (the monitor isn't running)


def slots() -> dict[str, int]:
    out = {"default": 3}
    for pair in config.HEAVY_SLOTS.split(","):
        name, _, n = pair.partition("=")
        if name.strip() and n.strip().isdigit():
            out[name.strip()] = int(n)
    return out


def _cls(name: str) -> str:
    return re.sub(r"[^a-z0-9-]", "-", name.lower()).strip("-") or "default"


# ------------------------------------------------------------- slots
class Slot:
    """A machine-wide slot: an flock on data/heavy/<class>.<n>.lock, held while the job runs."""

    def __init__(self, cls: str, n: int):
        self.path = DIR / f"{cls}.{n}.lock"
        self.fd: int | None = None

    def try_take(self, info: dict) -> bool:
        DIR.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps(info).encode())
        self.fd = fd
        return True

    def release(self) -> None:
        if self.fd is not None:
            os.ftruncate(self.fd, 0)
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None


def holders(cls: str | None = None) -> list[dict]:
    """Who holds slots now (the info each running job wrote into its slot file)."""
    out = []
    for p in sorted(DIR.glob("*.lock")) if DIR.exists() else []:
        if cls and not p.name.startswith(cls + "."):
            continue
        try:
            fd = os.open(p, os.O_RDONLY)
        except OSError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)  # nobody holds it
        except OSError:
            try:
                out.append({"slot": p.stem, **json.loads(p.read_text() or "{}")})
            except ValueError:
                out.append({"slot": p.stem})
        finally:
            os.close(fd)
    return out


def take_slot(cls: str, info: dict) -> Slot | None:
    for n in range(slots().get(cls, slots()["default"])):
        s = Slot(cls, n)
        if s.try_take(info):
            return s
    return None


# ------------------------------------------------------------- the gate
def gate() -> str | None:
    """Why a job must wait now (None: it may start)."""
    now = time.time()
    for path, key, bad in ((config.DATA_DIR / "health.json", "level", ("warn", "critical")),
                           (config.DATA_DIR / "disk.json", "level", ("full", "critical"))):
        try:
            state = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if now - state.get("ts", 0) < GATE_FRESH and state.get(key) in bad:
            what = "memory" if path.name == "health.json" else "disk"
            reasons = "; ".join(state.get("reasons", [])) or f"{state.get('pct', '?')}% used"
            return f"system health: {what} {state[key]} ({reasons})"
    return None


# ------------------------------------------------------------- cleanup
def _stop_unit(unit: str) -> None:
    subprocess.run(["systemctl", "--user", "stop", f"{unit}.scope"], capture_output=True, timeout=60)
    if shutil.which("docker"):
        ids = subprocess.run(["docker", "ps", "-q", "--filter", f"label=heavy.unit={unit}"],
                             capture_output=True, text=True, timeout=30).stdout.split()
        if ids:
            subprocess.run(["docker", "stop", "-t", "10", *ids], capture_output=True, timeout=120)


def sweep() -> list[str]:
    """Stop heavy scopes whose heavy process is gone (it was killed hard). Returns the units stopped."""
    stopped = []
    try:
        out = subprocess.run(["systemctl", "--user", "list-units", "--type=scope", "--state=running",
                              "--no-legend", "--plain", "heavy-*"], capture_output=True, text=True,
                             timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return stopped
    held = {h.get("unit") for h in holders()}
    for line in out.splitlines():
        unit = line.split()[0].removesuffix(".scope") if line.split() else ""
        if unit.startswith("heavy-") and unit not in held:
            _stop_unit(unit)
            stopped.append(unit)
    return stopped


# ------------------------------------------------------------- running
def _peak(pid: int) -> int:
    try:
        cg = Path(f"/proc/{pid}/cgroup").read_text().split(":", 2)[2].strip()
        return int(Path(f"/sys/fs/cgroup{cg}/memory.peak").read_text())
    except (OSError, ValueError, IndexError):
        return 0


def _log(rec: dict) -> None:
    try:
        DIR.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def run(cls: str, cmd: list[str], mem: str, swap: str, cpus: float | None, max_wait: float,
        force: bool, name: str = "") -> int:
    cls = _cls(cls)
    who = os.environ.get("WORKLOG_WORKER") or os.environ.get("USER", "?")
    unit = f"heavy-{cls}-{os.getpid()}"
    info = {"pid": os.getpid(), "unit": unit, "who": who, "name": name, "cmd": " ".join(cmd)[:300],
            "since": time.time()}
    sweep()
    started_wait, said = time.time(), ""
    slot = None
    while True:
        why = None if force else gate()
        if why is None:
            slot = take_slot(cls, info)
            if slot is None:
                busy = ", ".join(f"{h.get('who')} ({h.get('name') or h.get('cmd', '')[:60]})"
                                 for h in holders(cls)) or "?"
                why = f"all {slots().get(cls, slots()['default'])} {cls} slots are in use: {busy}"
        if why is None:
            break
        if why != said:
            print(f"heavy: waiting: {why}", file=sys.stderr, flush=True)
            said = why
        if time.time() - started_wait > max_wait:
            print(f"heavy: gave up after waiting {max_wait / 60:.0f} min", file=sys.stderr)
            return 75
        time.sleep(POLL)
    waited = time.time() - started_wait
    props = ["-p", f"MemoryMax={mem}", "-p", f"MemorySwapMax={swap}"]
    if cpus:
        props += ["-p", f"CPUQuota={int(cpus * 100)}%"]
    env = {**os.environ, "HEAVY_UNIT": unit, "HEAVY_CLASS": cls}

    def pdeathsig():  # the job gets SIGTERM if heavy dies
        try:
            import ctypes
            ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
        except OSError:
            pass

    rc, peak = 1, 0
    proc = subprocess.Popen(["systemd-run", "--user", "--scope", "--quiet", "--collect", "--unit", unit,
                             *props, "--", *cmd], env=env, preexec_fn=pdeathsig)

    def stop(signum, frame):
        _stop_unit(unit)

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, stop)
    try:
        while True:
            try:
                rc = proc.wait(timeout=POLL)
                break
            except subprocess.TimeoutExpired:
                peak = max(peak, _peak(proc.pid))
    finally:
        if proc.poll() is None:
            _stop_unit(unit)
            proc.wait()
            rc = proc.returncode
        else:
            _stop_unit(unit)  # what the job left running in its scope or in labelled containers
        slot.release()
        _log({**info, "class": cls, "end": time.time(), "waited_s": round(waited), "rc": rc,
              "peak_mib": peak // 1048576, "mem": mem})
    return rc


def status() -> str:
    lines = []
    for cls, n in sorted(slots().items()):
        held = holders(cls)
        lines.append(f"{cls}: {len(held)}/{n} slots in use")
        for h in held:
            age = int((time.time() - h.get("since", time.time())) / 60)
            lines.append(f"  {h.get('who')} for {age} min, {h.get('unit')}: {h.get('name') or h.get('cmd', '')[:100]}")
    why = gate()
    lines.append(f"gate: {'closed: ' + why if why else 'open'}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["status"]:
        print(status())
        return 0
    if argv[:1] == ["sweep"]:
        print("\n".join(sweep()) or "nothing to stop")
        return 0
    if "--" not in argv:
        print(__doc__, file=sys.stderr)
        return 2
    i = argv.index("--")
    ap = argparse.ArgumentParser(prog="heavy", add_help=True)
    ap.add_argument("--class", dest="cls", default="default", help="slot class (video-decode, gpu, ml-cpu, ...)")
    ap.add_argument("--mem", default=config.HEAVY_MEM, help="memory limit, e.g. 8G")
    ap.add_argument("--swap", default="1G", help="swap limit")
    ap.add_argument("--cpus", type=float, help="CPU limit in cores")
    ap.add_argument("--name", default="", help="a short label for status and the log")
    ap.add_argument("--max-wait", type=float, default=4 * 3600, help="seconds to wait for a slot and room")
    ap.add_argument("--force", action="store_true", help="start even if system health is warn")
    a = ap.parse_args(argv[:i])
    if not argv[i + 1:]:
        print("heavy: no command after --", file=sys.stderr)
        return 2
    return run(a.cls, argv[i + 1:], a.mem, a.swap, a.cpus, a.max_wait, a.force, a.name)


if __name__ == "__main__":
    sys.exit(main())

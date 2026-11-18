"""GPU flight recorder: what the NVIDIA GPU was doing when it failed.

Runs inside the uptime monitor (python -m harness.uptime), so it keeps
recording while the bot restarts. It does nothing if nvidia-smi isn't found.

- Samples: one line per second from a long-running `nvidia-smi -lms 1000`
  (power, temperature, clocks, utilization, memory, P-state, PCIe link, and the
  reasons the clocks are held back). The lines go to data/gpu/samples/<hour>.csv,
  synced to disk every few seconds, so they survive a hard crash or reset.
  Hour files older than SAMPLE_HOURS are deleted.
- Minutes: one summary line per minute, kept for good, in
  data/gpu/minutes-<month>.csv (min/mean/max power, max temperature, max
  utilization, the PCIe error counters, CPU temperature and load).
- Processes: the GPU processes (with command lines), logged when the set changes,
  in data/gpu/processes.log.
- Kernel watch (Linux): follows the kernel log. An NVIDIA Xid or a PCIe (AER)
  error is logged as a `gpu_error` event in data/uptime.jsonl (so /uptime shows
  it) and starts an incident report: data/gpu/incidents/<time>/ with the kernel
  lines, the last INCIDENT_SECONDS of samples, the GPU processes, the PCIe error
  counters and `nvidia-smi -q`.
- At startup, Xid errors from earlier boots that aren't logged yet (for example
  the machine froze before the report was written) are added from the journal,
  and an incident report is built from the samples that reached the disk.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

from . import config

log = logging.getLogger("gpumon")

DIR: Path = config.DATA_DIR / "gpu"
FIELDS = [
    "timestamp", "power.draw.instant", "power.draw.average", "power.limit",
    "temperature.gpu", "temperature.memory", "fan.speed", "clocks.current.graphics",
    "clocks.current.memory", "utilization.gpu", "memory.used", "pstate",
    "pcie.link.gen.current", "pcie.link.width.current", "clocks_event_reasons.active",
]
HEADER = ["time", "power_w", "power_avg_w", "power_limit_w", "temp_c", "mem_temp_c",
          "fan_pct", "clock_mhz", "mem_clock_mhz", "util_pct", "mem_used_mib", "pstate",
          "pcie_gen", "pcie_width", "clock_reasons"]
SAMPLE_MS = 1000
SAMPLE_HOURS = 48  # hours of 1 s samples to keep
SYNC_EVERY = 5.0  # seconds between fsyncs of the sample file
PROC_EVERY = 30.0  # seconds between GPU process checks
INCIDENT_SECONDS = 15 * 60  # samples put in an incident report
INCIDENT_GAP = 10 * 60  # kernel errors closer together than this share a report

# Kernel lines that mean trouble with the GPU or its PCIe link.
KERNEL_PATTERN = re.compile(
    r"NVRM: Xid|fallen off the bus|GPU is lost|AER:|PCIe Bus Error|pcieport.*(error|link down)"
    r"|nvidia-modeset: ERROR",
    re.IGNORECASE)
XID = re.compile(r"Xid \(PCI:([^)]+)\): (\d+)(?:, (.*))?")
# Xid codes worth naming in /uptime; others show as their number.
XID_NAMES = {
    79: "fell off the bus", 119: "GSP timeout", 120: "GSP error", 61: "internal micro-controller halt",
    62: "internal micro-controller halt", 48: "double-bit ECC error", 74: "NVLink error",
    13: "graphics engine exception", 31: "GPU memory page fault", 43: "GPU stopped processing",
    45: "preemptive cleanup", 94: "contained ECC error", 95: "uncontained ECC error",
}


def available() -> bool:
    return config.GPU_MONITOR and shutil.which("nvidia-smi") is not None


# --------------------------------------------------------------- helpers
def _hour_file(t: float) -> Path:
    return DIR / "samples" / (time.strftime("%Y%m%d-%H", time.localtime(t)) + ".csv")


def _prune_samples(now: float) -> None:
    for p in (DIR / "samples").glob("*.csv"):
        if now - p.stat().st_mtime > SAMPLE_HOURS * 3600:
            p.unlink(missing_ok=True)


def _parse_time(text: str) -> float | None:
    """nvidia-smi's '2026/11/17 20:08:21.426' as a unix time."""
    try:
        whole, _, frac = text.strip().partition(".")
        return time.mktime(time.strptime(whole, "%Y/%m/%d %H:%M:%S")) + float("0." + (frac or "0"))
    except ValueError:
        return None


def _num(v: str) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _gpu_pci() -> list[Path]:
    """sysfs folders of the NVIDIA GPUs and the PCIe ports above them."""
    out: list[Path] = []
    for dev in Path("/sys/bus/pci/devices").glob("*"):
        try:
            if (dev / "vendor").read_text().strip() == "0x10de" and \
                    (dev / "class").read_text().startswith("0x03"):
                out.append(dev)
                port = dev.resolve().parent
                if (port / "vendor").exists():
                    out.append(port)
        except OSError:
            continue
    return out


def aer_counters() -> dict[str, int]:
    """Total PCIe errors (correctable, nonfatal, fatal) per device, from sysfs."""
    out = {}
    for dev in _gpu_pci():
        for kind in ("correctable", "nonfatal", "fatal"):
            try:
                text = (dev / f"aer_dev_{kind}").read_text()
            except OSError:
                continue
            m = re.search(r"TOTAL_ERR_\w+ (\d+)", text)
            if m:
                out[f"{dev.name}:{kind}"] = int(m.group(1))
    return out


def _cpu_temp() -> float | None:
    for hw in Path("/sys/class/hwmon").glob("hwmon*"):
        try:
            if (hw / "name").read_text().strip() in ("coretemp", "k10temp", "zenpower"):
                return int((hw / "temp1_input").read_text()) / 1000
        except (OSError, ValueError):
            continue
    return None


def _gpu_processes() -> list[str]:
    try:
        r = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,used_memory",
                            "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return []
    procs = []
    for line in r.stdout.splitlines():
        pid, _, mem = (s.strip() for s in line.partition(","))
        try:
            cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            cmd = "?"
        procs.append(f"{pid} {mem}MiB {cmd.strip()[:300]}")
    return sorted(procs)


# --------------------------------------------------------------- recorder
class Recorder:
    def __init__(self, append_event: Callable[..., dict]):
        self.append_event = append_event
        self.minute: list[list[str]] = []
        self.minute_start = 0.0
        self.procs: list[str] = []
        self.last_incident = 0.0
        self.incident_dir: Path | None = None
        self.last_event: dict[object, float] = {}  # Xid code (or "other") -> time logged
        self._file = None
        self._file_path: Path | None = None
        self._last_sync = 0.0

    # samples
    def _write_sample(self, t: float, row: list[str]) -> None:
        path = _hour_file(t)
        if path != self._file_path:
            if self._file:
                self._file.close()
            path.parent.mkdir(parents=True, exist_ok=True)
            new = not path.exists()
            self._file = path.open("a", newline="")
            self._file_path = path
            if new:
                csv.writer(self._file).writerow(HEADER)
            _prune_samples(t)
        csv.writer(self._file).writerow([f"{t:.1f}", *row[1:]])
        if t - self._last_sync >= SYNC_EVERY:
            self._file.flush()
            os.fsync(self._file.fileno())
            self._last_sync = t

    def sample(self, line: str) -> None:
        row = [c.strip() for c in next(csv.reader([line]))]
        if len(row) != len(FIELDS):
            log.warning("nvidia-smi: %s", line.strip()[:200])
            return
        t = _parse_time(row[0]) or time.time()
        self._write_sample(t, row)
        if int(t // 60) != int(self.minute_start // 60) and self.minute:
            self._write_minute()
        if not self.minute:
            self.minute_start = t
        self.minute.append(row)

    def _write_minute(self) -> None:
        rows, self.minute = self.minute, []
        col = {name: i for i, name in enumerate(HEADER)}

        def values(name):
            return [v for v in (_num(r[col[name]]) for r in rows) if v is not None]

        power = values("power_w") or [0.0]
        reasons = 0
        for r in rows:
            try:
                reasons |= int(r[col["clock_reasons"]], 16)
            except ValueError:
                pass
        path = DIR / time.strftime("minutes-%Y-%m.csv", time.localtime(self.minute_start))
        new = not path.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["minute", "samples", "power_min_w", "power_mean_w", "power_max_w",
                            "temp_max_c", "util_max_pct", "mem_used_max_mib", "pcie_gen_min",
                            "clock_reasons_any", "pcie_errors", "cpu_temp_c", "load1"])
            w.writerow([
                time.strftime("%Y-%m-%d %H:%M", time.localtime(self.minute_start)), len(rows),
                f"{min(power):.0f}", f"{sum(power) / len(power):.0f}", f"{max(power):.0f}",
                f"{max(values('temp_c') or [0]):.0f}", f"{max(values('util_pct') or [0]):.0f}",
                f"{max(values('mem_used_mib') or [0]):.0f}", f"{min(values('pcie_gen') or [0]):.0f}",
                f"0x{reasons:x}", json.dumps(aer_counters(), separators=(",", ":")),
                _cpu_temp() or "", f"{os.getloadavg()[0]:.1f}" if hasattr(os, "getloadavg") else "",
            ])

    def flush(self) -> None:
        if self._file:
            self._file.flush()
            os.fsync(self._file.fileno())

    # processes
    def check_processes(self) -> None:
        procs = _gpu_processes()
        if procs != self.procs:
            self.procs = procs
            DIR.mkdir(parents=True, exist_ok=True)
            with (DIR / "processes.log").open("a") as f:
                stamp = time.strftime("%Y-%m-%d %H:%M:%S")
                f.write(f"{stamp} {len(procs)} GPU processes\n" + "".join(f"    {p}\n" for p in procs))

    # kernel errors
    def kernel_line(self, line: str, t: float | None = None, live: bool = True) -> None:
        if not KERNEL_PATTERN.search(line):
            return
        t = t or time.time()
        m = XID.search(line)
        key = int(m.group(2)) if m else "other"
        if t - self.last_event.get(key, 0) > INCIDENT_GAP:  # one event per kind per gap
            self.last_event[key] = t
            if m:
                self.append_event("gpu_error", ts=t, xid=key, pci=m.group(1),
                                  what=XID_NAMES.get(key, m.group(3) or ""), live=live)
            else:
                self.append_event("gpu_error", ts=t, what=line.strip()[-200:], live=live)
        if t - self.last_incident > INCIDENT_GAP:
            self.last_incident = t
            self.incident_dir = self._start_incident(t, live)
        if self.incident_dir:
            with (self.incident_dir / "kernel.log").open("a") as f:
                f.write(line.rstrip() + "\n")

    def _start_incident(self, t: float, live: bool) -> Path | None:
        d = DIR / "incidents" / time.strftime("%Y%m%d-%H%M%S", time.localtime(t))
        d.mkdir(parents=True, exist_ok=True)
        self.flush()
        # the samples before (and, for a past boot, up to) the error
        rows = 0
        with (d / "samples.csv").open("w", newline="") as out:
            w = csv.writer(out)
            w.writerow(HEADER)
            for h in range(int(INCIDENT_SECONDS // 3600) + 2, -1, -1):
                path = _hour_file(t - h * 3600)
                if not path.exists():
                    continue
                with path.open(newline="") as f:
                    for row in csv.reader(f):
                        st = _num(row[0]) if row else None
                        if st is not None and t - INCIDENT_SECONDS <= st <= t + 60:
                            w.writerow(row)
                            rows += 1
        if not live and not rows:  # from before the recorder ran: the event is enough
            shutil.rmtree(d, ignore_errors=True)
            return None
        (d / "processes.txt").write_text("\n".join(self.procs) + "\n" if live else
                                         "(see processes.log around this time)\n")
        (d / "pcie_errors.json").write_text(json.dumps(aer_counters(), indent=1))
        if live:
            try:
                q = subprocess.run(["nvidia-smi", "-q"], capture_output=True, text=True, timeout=20)
                (d / "nvidia-smi-q.txt").write_text(q.stdout + q.stderr)
            except (OSError, subprocess.TimeoutExpired) as e:
                (d / "nvidia-smi-q.txt").write_text(f"nvidia-smi -q failed: {e}\n")
        log.warning("GPU error: incident report in %s", d)
        return d


# --------------------------------------------------------------- tasks
async def _stream(cmd: list[str], on_line: Callable[[str], None], stop: asyncio.Event,
                  name: str) -> None:
    """Run a line-producing command until stop; restart it if it exits."""
    delay = 5
    while not stop.is_set():
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        except OSError as e:
            log.warning("%s: can't start: %s", name, e)
            return
        started = time.time()
        reader = asyncio.ensure_future(_read_lines(proc, on_line))
        stopper = asyncio.ensure_future(stop.wait())
        await asyncio.wait({reader, stopper}, return_when=asyncio.FIRST_COMPLETED)
        stopper.cancel()
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except asyncio.TimeoutError:
                proc.kill()
        reader.cancel()
        if stop.is_set():
            return
        log.warning("%s exited (rc %s); restarting in %ds", name, proc.returncode, delay)
        await asyncio.sleep(delay)
        delay = 5 if time.time() - started > 300 else min(delay * 2, 300)


async def _read_lines(proc, on_line) -> None:
    while True:
        line = await proc.stdout.readline()
        if not line:
            await proc.wait()
            return
        try:
            on_line(line.decode(errors="replace"))
        except Exception:
            log.exception("gpumon: failed on line %r", line[:200])


def backfill(rec: Recorder, logged: set[int]) -> None:
    """Add Xid errors from earlier boots of the journal that aren't logged yet."""
    if not shutil.which("journalctl"):
        return
    try:
        r = subprocess.run(["journalctl", "_TRANSPORT=kernel", "-o", "short-unix", "--no-pager", "-q",
                            "--since", "-30d", "--grep", r"NVRM: Xid"],
                           capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return
    for line in r.stdout.splitlines():
        stamp, _, rest = line.partition(" ")
        t = _num(stamp)
        if t is None or any(int(t) + d in logged for d in (-2, -1, 0, 1, 2)):
            continue
        logged.add(int(t))
        rec.kernel_line(rest, t=t, live=False)


async def run(stop: asyncio.Event, append_event: Callable[..., dict],
              logged_errors: set[int]) -> None:
    if not available():
        log.info("gpumon: off (no nvidia-smi, or GPU_MONITOR=0)")
        return
    rec = Recorder(append_event)
    await asyncio.to_thread(backfill, rec, logged_errors)
    rec.last_incident, rec.incident_dir = 0.0, None
    tasks = [asyncio.ensure_future(_stream(
        ["nvidia-smi", f"--query-gpu={','.join(FIELDS)}", "--format=csv,noheader,nounits",
         f"-lms={SAMPLE_MS}"], rec.sample, stop, "nvidia-smi sampler"))]
    if sys.platform.startswith("linux") and shutil.which("journalctl"):
        tasks.append(asyncio.ensure_future(_stream(
            ["journalctl", "-k", "-f", "-n", "0", "-o", "cat", "--no-pager"],
            rec.kernel_line, stop, "kernel log watch")))
    log.info("gpumon: recording to %s", DIR)
    while not stop.is_set():
        await asyncio.to_thread(rec.check_processes)
        try:
            await asyncio.wait_for(stop.wait(), PROC_EVERY)
        except asyncio.TimeoutError:
            pass
    await asyncio.gather(*tasks, return_exceptions=True)
    rec.flush()


def summary_lines(events: list[dict], now: float, days: int = 30) -> list[str]:
    """Lines for /uptime: GPU errors in the last `days` days."""
    errs = [e for e in events if e.get("event") == "gpu_error" and now - e["ts"] < days * 86400]
    if not errs:
        return []
    lines = ["", f"**GPU errors** (last {days} days): {len(errs)}"]
    for e in errs[-5:]:
        what = f"Xid {e['xid']} ({e.get('what') or '?'})" if "xid" in e else e.get("what", "")[:80]
        lines.append(f"• <t:{int(e['ts'])}:f> — {what}")
    return lines

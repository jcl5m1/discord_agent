"""Disk care: keep the main disk from filling up (part of the health monitoring).

Runs inside the uptime monitor. Every CHECK_EVERY seconds (and at once when the
main disk's level rises) it checks the disk that holds the home folder:

- at DISK_CLEAN_PCT (85%) used: clean caches and temporary files that are safe
  to delete (CLEANERS: package caches, thumbnails, old files in /tmp, dangling
  Docker images and build cache, stopped containers older than a week);
- at DISK_OFFLOAD_PCT (90%) used, if DISK_OFFLOAD_TO is set: move cold data to
  the secondary drive until the disk is under DISK_OFFLOAD_TARGET (80%). Cold
  data = a folder of at least OFFLOAD_MIN_GB under the home folder where no file
  changed for OFFLOAD_COLD_DAYS days, outside git work trees, config folders and
  folders a process has open. Each folder moves to the same path under
  DISK_OFFLOAD_TO and leaves a symlink, so paths keep working. Every action is
  logged in data/disk_actions.jsonl and as a `disk_action` event in uptime.jsonl.

The state (level, free space, the cold candidates, recent actions) goes to
data/disk.json, and the bot tells the health agent at warn.

CLI (the same steps by hand):
    python -m harness.diskcare status | scan | clean | offload <folder> | restore <folder>
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

from . import config, disk

log = logging.getLogger("diskcare")

STATE_PATH: Path = config.DATA_DIR / "disk.json"
ACTIONS_PATH: Path = config.DATA_DIR / "disk_actions.jsonl"
CHECK_EVERY = 600.0
SCAN_EVERY = 6 * 3600.0
OFFLOAD_MIN_GB = 1.0
OFFLOAD_COLD_DAYS = 14
TMP_OLD_DAYS = 2
SCAN_SECONDS = 300.0  # time limit for one scan of the home folder
# Count a file's ctime too: data copied in recently with old file times (rsync -a, tar) is not cold.
USE_CTIME = True
HOME = Path.home()
# Never moved: config, credentials, tools, the harness, and anything git tracks.
OFFLOAD_SKIP = {".ssh", ".gnupg", ".config", ".local", ".claude", ".kimi", ".docker", ".cache", ".sky",
                ".dstack", ".android", ".mozilla", ".pki", "snap", "Desktop", "github"}
# Cache folders under ~/.cache whose content tools rebuild on demand.
CACHE_DIRS = ["pip", "thumbnails", "go-build", "yarn", "node-gyp", "mesa_shader_cache", "tracker3", "yelp",
              "vllm", "pre-commit", "typescript", "ms-playwright-go", "electron", "fontconfig"]


# ------------------------------------------------------------- cleaning
def _tool(cmd: list[str], timeout: float = 600) -> str:
    if not shutil.which(cmd[0]):
        return "not installed"
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (r.stdout.strip().splitlines() or [r.stderr.strip() or f"rc {r.returncode}"])[-1][:200]
    except (OSError, subprocess.TimeoutExpired) as e:
        return str(e)[:200]


def _rm_tree_contents(path: Path) -> int:
    freed = 0
    try:
        entries = list(os.scandir(path))
    except OSError:
        return 0
    for e in entries:
        try:
            size = _size(Path(e.path))
            if e.is_dir(follow_symlinks=False):
                shutil.rmtree(e.path, ignore_errors=True)
            else:
                os.unlink(e.path)
            freed += size
        except OSError:
            continue
    return freed


def _size(p: Path) -> int:
    if p.is_symlink() or not p.is_dir():
        try:
            return p.lstat().st_size
        except OSError:
            return 0
    total = 0
    for dirpath, _, files in os.walk(p, onerror=lambda e: None):
        for f in files:
            try:
                total += os.lstat(os.path.join(dirpath, f)).st_size
            except OSError:
                pass
    return total


def _old_tmp(root: Path, days: float) -> int:
    """Delete this user's files and folders in root that nothing touched for `days` days."""
    freed, cutoff, uid = 0, time.time() - days * 86400, os.getuid()
    try:
        entries = list(os.scandir(root))
    except OSError:
        return 0
    for e in entries:
        try:
            st = e.stat(follow_symlinks=False)
            if st.st_uid != uid or max(st.st_mtime, st.st_atime) > cutoff:
                continue
            if e.is_dir(follow_symlinks=False):
                if _newest(Path(e.path), limit_s=10) > cutoff:
                    continue
                size = _size(Path(e.path))
                shutil.rmtree(e.path, ignore_errors=True)
            else:
                size = st.st_size
                os.unlink(e.path)
            freed += size
        except OSError:
            continue
    return freed


def clean() -> list[str]:
    """Run the safe cleaners; returns one line per cleaner."""
    out = []
    before = shutil.disk_usage(HOME).free
    for name in CACHE_DIRS:
        p = HOME / ".cache" / name
        if p.is_dir() and not p.is_symlink():
            freed = _rm_tree_contents(p)
            if freed:
                out.append(f"~/.cache/{name}: {disk.fmt_bytes(freed)}")
    out.append("uv cache prune: " + _tool(["uv", "cache", "prune"], timeout=60))  # skips if uv is busy
    for root in (Path("/tmp"), Path("/var/tmp")):
        freed = _old_tmp(root, TMP_OLD_DAYS)
        if freed:
            out.append(f"{root} (yours, untouched {TMP_OLD_DAYS}+ days): {disk.fmt_bytes(freed)}")
    if shutil.which("docker"):
        out.append("docker image prune (dangling): " + _tool(["docker", "image", "prune", "-f"]))
        out.append("docker builder prune: " + _tool(["docker", "builder", "prune", "-f"]))
        out.append("docker container prune (stopped 7+ days): "
                   + _tool(["docker", "container", "prune", "-f", "--filter", "until=168h"]))
    out.append(f"total freed: {disk.fmt_bytes(max(0, shutil.disk_usage(HOME).free - before))}")
    return out


# ------------------------------------------------------------- cold data
def _newest(p: Path, limit_s: float = 30) -> float:
    """The newest change time of any file under p (stops after limit_s seconds: then 'now')."""
    end, newest = time.monotonic() + limit_s, 0.0
    for dirpath, dirs, files in os.walk(p, onerror=lambda e: None):
        if time.monotonic() > end:
            return time.time()
        for name in files + dirs:
            try:
                st = os.lstat(os.path.join(dirpath, name))
                newest = max(newest, st.st_mtime, st.st_ctime if USE_CTIME else 0)
            except OSError:
                pass
    return newest


def _in_git(p: Path) -> bool:
    for q in (p, *p.parents):
        if (q / ".git").exists():
            return True
        if q == HOME:
            break
    return False


def _open_paths() -> set[str]:
    """Folders processes have open (their cwd and open files), to leave alone."""
    out = set()
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            out.add(os.readlink(proc / "cwd"))
            for fd in (proc / "fd").iterdir():
                target = os.readlink(fd)
                if target.startswith("/"):
                    out.add(target)
        except OSError:
            continue
    return out


def scan(limit_s: float = SCAN_SECONDS) -> list[dict]:
    """Cold folders worth moving, biggest first: {path, gb, days}."""
    end = time.monotonic() + limit_s
    cutoff = time.time() - OFFLOAD_COLD_DAYS * 86400
    found: list[dict] = []
    in_use = _open_paths()

    def busy(p: str) -> bool:
        return any(o == p or o.startswith(p + "/") for o in in_use)

    def walk(d: Path, depth: int) -> None:
        if time.monotonic() > end or depth > 3:
            return
        try:
            subs = [Path(e.path) for e in os.scandir(d) if e.is_dir(follow_symlinks=False)]
        except OSError:
            return
        for s in subs:
            if depth == 0 and s.name in OFFLOAD_SKIP:
                continue
            if _in_git(s) or (s / "pyvenv.cfg").exists() or busy(str(s)):
                continue
            newest = _newest(s, limit_s=20)
            if newest and newest < cutoff:
                size = _size(s)
                if size >= OFFLOAD_MIN_GB * 1024 ** 3:
                    found.append({"path": str(s), "gb": round(size / 1024 ** 3, 1),
                                  "days": int((time.time() - newest) / 86400)})
                continue  # cold as a whole: don't split it
            walk(s, depth + 1)

    walk(HOME, 0)
    return sorted(found, key=lambda c: -c["gb"])


def offload(path: str, dest_root: str | None = None) -> str:
    """Move a folder to the secondary drive and leave a symlink in its place."""
    src = Path(path).expanduser().resolve()
    dest_root = dest_root or config.DISK_OFFLOAD_TO
    if not dest_root:
        raise SystemExit("DISK_OFFLOAD_TO is not set")
    if HOME not in src.parents:
        raise SystemExit(f"{src} is not under {HOME}")
    if src.is_symlink() or not src.is_dir():
        raise SystemExit(f"{src} is not a folder")
    dest = Path(dest_root) / src.relative_to(HOME)
    if dest.exists():
        raise SystemExit(f"{dest} exists already")
    size = _size(src)
    if shutil.disk_usage(dest_root).free < size * 1.1:
        raise SystemExit(f"not enough space on {dest_root}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["rsync", "-a", "--remove-source-files", f"{src}/", f"{dest}/"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"rsync failed ({r.returncode}): {r.stderr[-300:]}; nothing removed that wasn't copied")
    shutil.rmtree(src, ignore_errors=True)  # only empty folders are left after --remove-source-files
    if src.exists():
        raise SystemExit(f"copied to {dest}, but {src} still has files: check it")
    src.symlink_to(dest, target_is_directory=True)
    line = f"moved {src} ({disk.fmt_bytes(size)}) to {dest}; symlink left"
    _record("offload", line, path=str(src), dest=str(dest), bytes=size)
    return line


def restore(path: str) -> str:
    """Move an offloaded folder back (the symlink is replaced by the folder)."""
    link = Path(path).expanduser()
    if not link.is_symlink():
        raise SystemExit(f"{link} is not an offload symlink")
    dest = Path(os.readlink(link))
    link.unlink()
    r = subprocess.run(["rsync", "-a", "--remove-source-files", f"{dest}/", f"{link}/"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"rsync failed: {r.stderr[-300:]}")
    shutil.rmtree(dest, ignore_errors=True)
    line = f"restored {link} from {dest}"
    _record("restore", line, path=str(link), dest=str(dest))
    return line


# ------------------------------------------------------------- records and the loop
_append_event: Callable[..., dict] | None = None


def _record(action: str, text: str, **fields) -> None:
    rec = {"ts": time.time(), "action": action, "text": text, **fields}
    try:
        with ACTIONS_PATH.open("a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass
    log.warning("disk care: %s", text)
    if _append_event:
        _append_event("disk_action", action=action, text=text[:300])


def read_state() -> dict | None:
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        return None


def _write_state(state: dict) -> None:
    try:
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        tmp.replace(STATE_PATH)
    except OSError:
        pass


def step(state: dict, now: float, scan_now: bool = False) -> dict:
    """One check: clean and offload as the levels say; returns the new state."""
    pct, free = disk.usage(HOME)
    level = disk.level(pct, free, state.get("level", "ok"))
    actions = list(state.get("actions", []))[-20:]
    if pct >= config.DISK_CLEAN_PCT and now - state.get("cleaned", 0) > 3600:
        lines = clean()
        _record("clean", "; ".join(lines))
        actions.append({"ts": now, "text": "cleaned: " + "; ".join(lines)})
        state["cleaned"] = now
        pct, free = disk.usage(HOME)
    candidates = state.get("candidates", [])
    if scan_now or now - state.get("scanned", 0) > SCAN_EVERY or (pct >= config.DISK_WARN_PCT and not candidates):
        candidates = scan()
        state["scanned"] = now
    if config.DISK_OFFLOAD_TO and pct >= config.DISK_OFFLOAD_PCT:
        for c in list(candidates):
            if pct < config.DISK_OFFLOAD_TARGET:
                break
            try:
                actions.append({"ts": now, "text": offload(c["path"])})
            except SystemExit as e:
                actions.append({"ts": now, "text": f"could not move {c['path']}: {e}"})
                log.warning("disk care: %s", e)
            candidates.remove(c)
            pct, free = disk.usage(HOME)
    level = disk.level(pct, free, level)
    return {**state, "ts": now, "path": str(HOME), "level": level, "pct": round(pct, 1), "free": free,
            "candidates": candidates[:15], "actions": actions[-20:]}


async def run(stop, append_event: Callable[..., dict] | None = None) -> None:
    import asyncio

    global _append_event
    if not sys.platform.startswith("linux") or not config.DISK_CARE:
        return
    _append_event = append_event
    state = read_state() or {}
    log.info("diskcare: checking %s every %ds (clean at %.0f%%, move cold data at %.0f%% to %s)", HOME,
             CHECK_EVERY, config.DISK_CLEAN_PCT, config.DISK_OFFLOAD_PCT, config.DISK_OFFLOAD_TO or "(off)")
    last = 0.0
    while not stop.is_set():
        try:
            pct, free = disk.usage(HOME)
            rising = disk.LEVELS.index(disk.level(pct, free, state.get("level", "ok"))) > \
                disk.LEVELS.index(state.get("level", "ok"))
            if rising or time.time() - last >= CHECK_EVERY:
                state = await asyncio.to_thread(step, state, time.time())
                _write_state(state)
                last = time.time()
        except Exception:
            log.exception("disk care failed")
        try:
            await asyncio.wait_for(stop.wait(), 30)
        except asyncio.TimeoutError:
            pass


def main(argv: list[str]) -> None:
    cmd = argv[0] if argv else "status"
    if cmd == "status":
        pct, free = disk.usage(HOME)
        s = read_state() or {}
        print(f"{HOME}: {pct:.1f}% used, {disk.fmt_bytes(free)} free; level {s.get('level', '?')}")
        for a in s.get("actions", [])[-5:]:
            print(f"  [{time.strftime('%m-%d %H:%M', time.localtime(a['ts']))}] {a['text'][:200]}")
    elif cmd == "scan":
        for c in scan():
            print(f"{c['gb']:7.1f} GB  {c['days']:4d} days unchanged  {c['path']}")
    elif cmd == "clean":
        print("\n".join(clean()))
    elif cmd == "offload" and len(argv) > 1:
        print(offload(argv[1]))
    elif cmd == "restore" and len(argv) > 1:
        print(restore(argv[1]))
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])

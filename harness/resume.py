"""Turns cut off by a harness restart, so they resume when the harness is back.

A turn lasts as long as the agent's CLI process: its reply, and also any
background subagents or tasks that keep the process open after the reply. A
restart (systemd, /update, a crash) kills that process. Each running turn is
recorded here when it starts and removed when it ends; a restart skips the
removal, so the record is left. At start-up the harness takes the records and
gives each channel a resume turn that says what was cut off.

`python -m harness.resume restart` records the turns running now from the process table
(each agent CLI under the harness, matched to its instance by session id), then restarts
the systemd unit. Use it for a restart from a terminal, or when the running harness is
older than this module and keeps no records.

A record also counts how many resumes in a row it has had. A turn that keeps
being cut off (e.g. it crashes the harness) stops after MAX_RESUMES, with a
note in the channel instead of another resume.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import config

PATH: Path = config.DATA_DIR / "interrupted_turns.json"
MAX_RESUMES = 2
PREVIEW = 600


def mark(channel_id: int, instance: str, prompt: str, message_id: int | None = None,
         resumes: int = 0) -> None:
    records = _load()
    records[str(channel_id)] = {
        "instance": instance,
        "started": time.time(),
        "message_id": message_id,
        "prompt": prompt[:PREVIEW],
        "resumes": resumes,
    }
    _save(records)


def clear(channel_id: int) -> None:
    records = _load()
    if records.pop(str(channel_id), None) is not None:
        _save(records)


def take() -> dict[int, dict]:
    """Every record left by the last run, and forget them."""
    records = _load()
    if records:
        _save({})
    return {int(k): v for k, v in records.items()}


def prompt(record: dict, now: float | None = None) -> str:
    started = time.strftime("%H:%M", time.localtime(record.get("started") or 0))
    restarted = time.strftime("%H:%M", time.localtime(now or time.time()))
    request = (record.get("prompt") or "").strip()
    if record.get("resumes"):
        request = "(an earlier resume, which was cut off too)\n" + request
    return (
        f"[harness restart] The harness restarted at {restarted} and stopped your last turn "
        f"(started {started}) before it ended. Your session is kept, so you remember the work. "
        "Background subagents, tasks and commands of that turn were stopped too; servers started "
        "with setsid nohup and systemd units keep running. Check the state of the work (wl status "
        "and your claims, files, git, rented instances), then continue it where it stopped. "
        "Re-claim worklog tasks that dropped. If the work was already finished, say so in one line.\n\n"
        f"The request of the stopped turn was:\n{request}"
    )


def _load() -> dict:
    try:
        return json.loads(PATH.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _save(records: dict) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(records, indent=1))
    tmp.replace(PATH)


def record_running() -> list[str]:
    """Record a turn for each agent CLI the running harness has open (by session id)."""
    sessions = {}
    try:
        instances = json.loads((config.DATA_DIR / "instances.json").read_text())
    except (FileNotFoundError, ValueError):
        return []
    for cid, inst in instances.items():
        if inst.get("session_id"):
            sessions[inst["session_id"]] = (int(cid), inst.get("name", cid))
    harness = _harness_pid()
    found = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            ppid = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        args = [a.decode(errors="replace") for a in argv if a]
        if harness and ppid != harness:
            continue
        session = next((args[i + 1] for i, a in enumerate(args[:-1]) if a in ("--resume", "--session")), None)
        if session in sessions:
            channel_id, name = sessions[session]
            if str(channel_id) not in _load():
                mark(channel_id, name, "(recorded from the running process before a restart; "
                                       "the request text is in your session)")
            found.append(name)
    return found


def _harness_pid() -> int | None:
    out = subprocess.run(["systemctl", "--user", "show", "-p", "MainPID", "--value", "discord-agent.service"],
                         capture_output=True, text=True).stdout.strip()
    return int(out) if out.isdigit() and out != "0" else None


def main(argv: list[str]) -> None:
    if argv[:1] != ["restart"]:
        raise SystemExit("usage: python -m harness.resume restart   # record running turns, restart the harness")
    found = record_running()
    print("recorded running turns: " + (", ".join(found) if found else "none"))
    subprocess.run(["systemctl", "--user", "restart", "discord-agent.service"], check=True)
    print("harness restarted; recorded turns resume when it is back")


if __name__ == "__main__":
    main(sys.argv[1:])

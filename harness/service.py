"""Keep the systemd user units for the harness in sync with this checkout.

The unit files are rendered from the templates below (repo path, venv python and
PATH filled in for this machine) and rewritten only when the installed copy
differs, followed by `systemctl --user daemon-reload`. The bot runs this on
startup and /update runs it from the freshly pulled code before restarting, so
a `git pull` is all a machine needs to pick up unit changes.

Only acts when the harness itself runs as the user unit `discord-agent.service`;
a harness started any other way (run.sh in a terminal, a differently named or
system-level unit) is left alone.

    python -m harness.service    # sync now and print what changed
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from . import config

UNIT_DIR = Path.home() / ".config" / "systemd" / "user"
BOT_UNIT = "discord-agent.service"
WATCHDOG_UNIT = "discord-agent-watchdog.service"
UPTIME_UNIT = "discord-agent-uptime.service"
# systemd's default PATH; the user bin dirs from config are put in front of it.
_SYSTEM_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin"

_TEMPLATES = {
    BOT_UNIT: """\
# Managed by harness/service.py — local edits are overwritten on the next sync.
[Unit]
Description=Discord agent harness (bot)
After=network-online.target
Wants=network-online.target
# Bring the watchdog up (and down) with the bot.
Wants={watchdog_unit}

[Service]
Type=simple
WorkingDirectory={repo}
# claude, muse and kimi live in user bin dirs that systemd's default PATH omits.
Environment=PATH={path}
ExecStart={python} -m harness.bot
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
""",
    WATCHDOG_UNIT: """\
# Managed by harness/service.py — local edits are overwritten on the next sync.
[Unit]
Description=Discord agent harness watchdog (alerts when the harness dies or a turn stalls)
# Bound to the bot: starts after it, and stops when the bot unit is stopped.
BindsTo={bot_unit}
After={bot_unit}

[Service]
Type=simple
WorkingDirectory={repo}
Environment=PATH={path}
ExecStart={python} -m harness.watchdog
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
""",
    UPTIME_UNIT: """\
# Managed by harness/service.py — local edits are overwritten on the next sync.
[Unit]
Description=Discord agent harness uptime log (internet + system up/down events)
# Deliberately not bound to the bot: it must keep logging while the harness is down.

[Service]
Type=simple
WorkingDirectory={repo}
Environment=PATH={path}
ExecStart={python} -m harness.uptime
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
""",
}


def current_unit() -> str | None:
    """Name of the systemd user unit running this process, or None."""
    if not os.environ.get("INVOCATION_ID"):
        return None
    try:
        cgroup = Path("/proc/self/cgroup").read_text()
    except OSError:
        return None
    if "/user@" not in cgroup:  # a system-level unit; not ours to rewrite
        return None
    for part in reversed(cgroup.strip().split("/")):
        if part.endswith(".service"):
            return part
    return None


def render() -> dict[str, str]:
    path = ":".join([*config.USER_BIN_DIRS, _SYSTEM_PATH])
    # The venv's python path, not its resolved interpreter: the venv must stay active.
    python = str(config.BASE_DIR / ".venv" / "bin" / "python")
    if not os.path.exists(python):
        python = sys.executable
    return {
        name: tpl.format(
            repo=config.BASE_DIR, python=python, path=path,
            bot_unit=BOT_UNIT, watchdog_unit=WATCHDOG_UNIT,
        )
        for name, tpl in _TEMPLATES.items()
    }


def sync(unit: str | None = None) -> list[str]:
    """Rewrite installed units that differ from the templates; return their names.

    unit is the unit this process runs as (default: detected). Nothing happens
    unless it is the managed bot unit.
    """
    if (unit if unit is not None else current_unit()) != BOT_UNIT:
        return []
    changed = []
    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    for name, text in render().items():
        dest = UNIT_DIR / name
        if dest.exists() and dest.read_text() == text:
            continue
        dest.write_text(text)
        changed.append(name)
    if changed:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        # A watchdog unit installed for the first time must also start at login.
        subprocess.run(["systemctl", "--user", "enable", "--quiet", *_TEMPLATES], check=False)
        # The uptime unit runs independently of the bot, so (re)start it here:
        # nothing else would pick up a freshly installed or changed unit.
        if UPTIME_UNIT in changed:
            subprocess.run(["systemctl", "--user", "--no-block", "restart", UPTIME_UNIT], check=False)
    return changed


if __name__ == "__main__":
    # /update runs this in a child process of the bot, which shares its cgroup.
    changed = sync()
    print("updated: " + ", ".join(changed) if changed else "unchanged")

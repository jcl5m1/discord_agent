"""Log files for the harness's processes, and the export behind /logs.

Each process (bot, watchdog, uptime) writes data/logs/<name>.log, rotated at
5 MB with 3 old files kept, so logs survive restarts on every OS (on Windows
the logon tasks have no console to read afterwards). /logs bundles the recent
lines, plus systemd's journal where there is one, into one file with anything
that looks like a secret removed, so it can be shared from Discord.
"""

from __future__ import annotations

import logging
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import config

LOG_DIR: Path = config.DATA_DIR / "logs"
FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
PROCESSES = ("bot", "watchdog", "uptime")
MAX_BYTES = 5_000_000
BACKUPS = 3


def setup(name: str) -> None:
    """Log to the console and to data/logs/<name>.log.

    force=True: a library imported earlier (neonize) may already have
    configured logging, which would otherwise make this a silent no-op.
    """
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(
            LOG_DIR / f"{name}.log", maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8"
        ))
    except OSError as e:
        print(f"log file unavailable ({e}); logging to the console only", file=sys.stderr)
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL, logging.INFO),
        format=FORMAT,
        handlers=handlers,
        force=True,
    )


# Secrets that could end up in a log line: Discord tokens, API keys, bearer
# tokens, and key=value / "key": "value" pairs with secret-sounding names.
_SECRET_PATTERNS = [
    re.compile(r"[MNO][A-Za-z\d_-]{23,27}\.[A-Za-z\d_-]{6}\.[A-Za-z\d_-]{27,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(
        r"(?i)(\"?(?:access_token|refresh_token|api_key|apikey|client_secret|secret|token|password)\"?"
        r"\s*[:=]\s*\"?)[^\"\s,}]{8,}"
    ),
]


def redact(text: str) -> str:
    for secret in (config.BOT_TOKEN, *(os.environ.get(v, "") for v in config.AGENT_API_KEY_VARS)):
        if secret and len(secret) >= 8:
            text = text.replace(secret, "[redacted]")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda m: (m.group(1) if m.re.groups else "") + "[redacted]", text)
    return text


def _tail(name: str, lines: int) -> list[str]:
    """The last `lines` lines of <name>.log, reading older rotated files as needed."""
    files = [LOG_DIR / f"{name}.log.{i}" for i in range(BACKUPS, 0, -1)] + [LOG_DIR / f"{name}.log"]
    out: list[str] = []
    for path in files:
        try:
            out += path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
    return out[-lines:]


def _journal(lines: int) -> str:
    if not shutil.which("journalctl"):
        return ""
    try:
        return subprocess.run(
            ["journalctl", "--user", "--no-pager", "-o", "short-iso", "-n", str(lines),
             "-u", "discord-agent.service", "-u", "discord-agent-watchdog.service",
             "-u", "discord-agent-uptime.service"],
            capture_output=True, text=True, timeout=30,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _git_head() -> str:
    try:
        return subprocess.run(
            ["git", "log", "-1", "--format=%h %cd %s", "--date=short"],
            cwd=config.BASE_DIR, capture_output=True, text=True, timeout=10,
        ).stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def export(lines: int, header: list[str] | None = None) -> str:
    """Everything /logs shares, as one redacted text file."""
    parts = [
        f"# discord_agent logs from {socket.gethostname()} ({platform.platform()})",
        f"# exported {time.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"# harness {_git_head()} · python {platform.python_version()}",
        *(f"# {line}" for line in header or []),
        "",
    ]
    for name in PROCESSES:
        tail = _tail(name, lines)
        parts += [f"## {name}.log (last {len(tail)} lines)", *tail, ""]
    journal = _journal(lines)
    if journal:
        parts += ["## systemd journal", journal]
    return redact("\n".join(parts))

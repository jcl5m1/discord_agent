"""Agent CLI sign-in from Discord (/login), using each CLI's own subscription login.

The harness never handles credentials itself. It runs the CLI's login command,
passes the sign-in link (and the code to confirm) to Discord, and for Claude
relays the code you paste back. Each CLI then stores its own login as usual:

    claude  claude auth login --claudeai   link, then paste the code it shows
    kimi    kimi login                     device code, approve in the browser
    muse    muse login                     device code, approve in the browser
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import config
from .providers.base import run_text

PROVIDERS = ("claude", "kimi", "muse")
LABELS = {"claude": "Claude Code", "kimi": "Kimi Code", "muse": "Muse"}
START_TIMEOUT = 30  # seconds to wait for the sign-in link
LOGIN_TIMEOUT = 15 * 60  # device codes and pasted codes expire well before this

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_URL_RE = re.compile(r"https://[^\s<>\"']+")
_CONFIRM_CODE_RE = re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{4}\b")
_PASTE_PROMPT = "Paste code"
# Claude prints its result on the same line as this prompt.
_PROMPT_RE = re.compile(r"Paste code[^>]*>\s*")


def _bin(name: str) -> str:
    path = {"claude": config.CLAUDE_BIN, "kimi": config.KIMI_BIN, "muse": config.MUSE_BIN}[name]
    return shutil.which(path) or path


def _installed(name: str) -> bool:
    return os.path.isfile(_bin(name))


def login_command(name: str) -> list[str]:
    if name == "claude":
        return [_bin(name), "auth", "login", "--claudeai"]
    return [_bin(name), "login"]


def _muse_auth_path() -> Path:
    if os.environ.get("MUSE_AUTH_PATH"):
        return Path(os.environ["MUSE_AUTH_PATH"])
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(Path.home(), ".config")
    return Path(base) / "muse" / "auth.json"


@dataclass
class Status:
    installed: bool
    logged_in: bool
    subscription: bool
    detail: str


async def status(name: str) -> Status:
    """Whether the CLI is signed in, and with a subscription rather than an API key."""
    if not _installed(name):
        return Status(False, False, False, "not installed")
    try:
        if name == "claude":
            info = json.loads(await run_text(_bin(name), "auth", "status", "--json", timeout=30))
            if not info.get("loggedIn"):
                return Status(True, False, False, "not logged in")
            if info.get("authMethod") == "claude.ai":
                plan = info.get("subscriptionType")
                return Status(True, True, True, f"subscription{f' ({plan})' if plan else ''}")
            return Status(True, True, False, f"API billing ({info.get('authMethod', 'unknown')})")
        if name == "kimi":
            providers = await run_text(_bin(name), "provider", "list", timeout=30)
            if "source=oauth" in providers:
                return Status(True, True, True, "subscription")
            if "source=" in providers:
                return Status(True, True, False, "API key provider")
            return Status(True, False, False, "not logged in")
        path = _muse_auth_path()
        if not path.exists():
            return Status(True, False, False, "not logged in")
        mechanisms = {p.get("mechanism") for p in json.loads(path.read_text()).get("providers", {}).values()}
        if "oauth" in mechanisms:
            return Status(True, True, True, "subscription")
        return Status(True, bool(mechanisms), False, "API key" if mechanisms else "not logged in")
    except (RuntimeError, OSError, ValueError) as e:
        # e.g. a CLI that exits non-zero when logged out
        return Status(True, False, False, f"not logged in ({str(e).splitlines()[0][:80]})")


class LoginError(Exception):
    pass


class LoginSession:
    """One running `<cli> login`, from its sign-in link to its exit."""

    def __init__(self, name: str, command: list[str] | None = None):
        self.name = name
        self.command = command or login_command(name)
        self.output = ""
        self.url: str | None = None
        self.confirm_code: str | None = None
        self.needs_code = False
        self.proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._changed = asyncio.Event()

    @property
    def done(self) -> bool:
        return self.proc is not None and self.proc.returncode is not None

    async def start(self, timeout: float = START_TIMEOUT) -> None:
        env = dict(os.environ)
        if os.name != "nt":
            env["BROWSER"] = "true"  # don't open a browser on this machine
        self.proc = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        self._reader = asyncio.get_running_loop().create_task(self._read())
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        # Wait for the link, then briefly for what follows it (the code to
        # confirm, or Claude's paste prompt).
        while not (self.url and (self.needs_code or self.confirm_code)):
            remaining = deadline - loop.time()
            if remaining <= 0 or (self.done and not self.url):
                break
            if self.url:
                remaining = min(remaining, 3)
            self._changed.clear()
            try:
                await asyncio.wait_for(self._changed.wait(), remaining)
            except asyncio.TimeoutError:
                if self.url:
                    break
        if not self.url:
            self.cancel()
            await self.proc.wait()
            tail = self.output.strip()[-300:] or "no output"
            raise LoginError(f"{self.name} login didn't give a sign-in link: {tail}")

    async def _read(self) -> None:
        assert self.proc and self.proc.stdout
        while True:
            chunk = await self.proc.stdout.read(4096)
            if not chunk:
                break
            self.output += _ANSI.sub("", chunk.decode("utf-8", "replace"))
            if not self.url and (m := _URL_RE.search(self.output)):
                self.url = m.group(0)
            if not self.confirm_code and (m := _CONFIRM_CODE_RE.search(self.output)):
                self.confirm_code = m.group(0)
            if _PASTE_PROMPT in self.output:
                self.needs_code = True
            self._changed.set()
        self._changed.set()

    async def submit_code(self, code: str) -> None:
        if not self.proc or not self.proc.stdin or self.done:
            raise LoginError("that sign-in already ended — start again")
        self.proc.stdin.write(code.strip().encode() + b"\n")
        await self.proc.stdin.drain()

    async def wait(self, timeout: float = LOGIN_TIMEOUT) -> bool:
        """True if the login command exited successfully."""
        assert self.proc
        try:
            await asyncio.wait_for(self.proc.wait(), timeout)
        except asyncio.TimeoutError:
            self.cancel()
            await self.proc.wait()
            return False
        if self._reader:
            await self._reader
        return self.proc.returncode == 0

    def result_text(self) -> str:
        """The CLI's last words after the link, for reporting a failure."""
        after = self.output.split(self.url, 1)[-1] if self.url else self.output
        lines = [l for l in (_PROMPT_RE.sub("", l).strip() for l in after.splitlines()) if l]
        return lines[-1][:200] if lines else ""

    def cancel(self) -> None:
        if self.proc and self.proc.returncode is None:
            self.proc.kill()

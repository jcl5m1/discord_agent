"""Provider abstraction: a coding-agent CLI the harness can drive turn-by-turn.

A provider runs one prompt as a subprocess (non-interactive CLI mode) and yields
text events as they arrive. Each agent instance keeps whatever session identifier
the provider returns, so instances survive harness restarts as long as the CLI's
own session store does.
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncIterator


class TurnControl:
    """Acts on a running turn, for providers with supports_live_input.

    send() delivers a message into the turn (the agent reads it at its next
    step, the way the interactive CLI's queue works); interrupt() stops the turn
    gracefully (like pressing Esc) and keeps the session.
    """

    def __init__(self) -> None:
        self.requests: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        # Sent but not yet read by the agent. Filled here, not when the provider
        # writes it, so a turn ending in between still knows to wait for it.
        self.unread: set[str] = set()

    def send(self, text: str) -> str:
        import uuid

        message_id = str(uuid.uuid4())
        self.unread.add(message_id)
        self.requests.put_nowait(("message", f"{message_id}\n{text}"))
        return message_id

    def interrupt(self) -> None:
        self.requests.put_nowait(("interrupt", ""))


@dataclass
class TurnResult:
    """Result of one provider turn."""

    text: str = ""  # full assistant text for the turn
    session_id: str | None = None  # provider session id (capture on first turn)
    error: str | None = None


@dataclass
class StreamEvent:
    """Incremental event while a turn is running.

    kind is one of: "text", "status", "session", "delivered", "interrupted",
    "done", "error".
    "session" carries the provider session id as soon as it's known, so a turn
    that's interrupted before "done" can still be resumed.
    "delivered" (text = the id TurnControl.send returned): the agent has read a
    message sent into the running turn. "interrupted": the turn stopped because
    TurnControl.interrupt asked it to.
    "done" events carry the final text and, when known, the provider session id.
    """

    kind: str
    text: str = ""
    session_id: str | None = None


@dataclass
class SessionInfo:
    """A session in the CLI's own store, offered by /session."""

    id: str
    workdir: str  # cwd the session belongs to; resuming needs the same cwd
    title: str = ""
    updated: float = 0.0  # unix seconds, for newest-first ordering


class AgentProvider(ABC):
    """Interface every agent CLI integration must implement."""

    name: str = "abstract"
    # Short model aliases -> full model IDs (offered as choices in Discord).
    models: dict[str, str] = {}
    # Model used when an instance has none set (None = the CLI's own default).
    default_model: str | None = None

    def resolve_model(self, model: str | None) -> str | None:
        """Map an alias (or None) to the model ID passed to the CLI."""
        model = model or self.default_model
        return self.models.get(model, model) if model else None

    async def usage(self) -> str:
        """Plan/quota status as the CLI reports it (shown by /usage)."""
        return f"`{self.name}` has no usage command"

    def sessions(self) -> list[SessionInfo]:
        """Sessions in the CLI's store, newest first ([] = can't list them)."""
        return []

    def find_session(self, session_id: str) -> SessionInfo | None:
        return next((s for s in self.sessions() if s.id == session_id), None)

    # True when run_turn accepts control= (a TurnControl) for live messages and
    # graceful interrupts; otherwise messages wait for the turn and an interrupt
    # kills the CLI.
    supports_live_input: bool = False

    @abstractmethod
    def run_turn(
        self, workdir: str, prompt: str, session_id: str | None = None, model: str | None = None
    ) -> AsyncIterator[StreamEvent]:
        """Run one prompt against the agent CLI.

        If session_id is given, resume that session; otherwise start a new one.
        model is the instance's model (alias or ID); None means default_model.
        Yields StreamEvents as output arrives, ending with a "done" or "error"
        event whose text carries the final text / error message.
        """


# Agent CLIs get their own process group (POSIX) / group (Windows), so stopping
# a turn can kill the CLI and everything it started without touching the harness.
SPAWN_KWARGS: dict = (
    {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
    else {"start_new_session": True}
)


def _descendants(pid: int) -> list[int]:
    """PIDs below pid in the process tree, from /proc (Linux); [] elsewhere.

    Agent CLIs run tool commands in process groups of their own (Claude Code's
    Bash tool does), so killing the CLI's group alone would leave them running.
    """
    children: dict[int, list[int]] = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return []
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat") as f:
                ppid = int(f.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        children.setdefault(ppid, []).append(int(entry))
    found, todo = [], [pid]
    while todo:
        for child in children.get(todo.pop(), []):
            found.append(child)
            todo.append(child)
    return found


def _signal_all(pids: list[int], sig: int) -> None:
    for pid in pids:
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


async def kill_process_tree(proc: asyncio.subprocess.Process, grace: float = 3.0) -> None:
    """Stop an agent CLI that's still running, with everything it started.

    A no-op once the CLI has exited, so finished turns keep anything they left
    running in the background. Servers started detached (setsid nohup ... &)
    have already left the tree, so they keep running too. On Windows, killing a
    .cmd launcher alone would leave the real CLI running, so the whole tree is
    killed.
    """
    if proc.returncode is not None:
        return
    try:
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/T", "/F", "/PID", str(proc.pid),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        else:
            # Find the tree before killing: once the CLI dies its children are
            # re-parented and the link to them is lost.
            tree = [proc.pid, *_descendants(proc.pid)]
            try:
                os.killpg(proc.pid, signal.SIGTERM)  # pgid == pid: it leads its own session
            except ProcessLookupError:
                pass
            _signal_all(tree, signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), grace)
            except asyncio.TimeoutError:
                pass
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            _signal_all(tree, signal.SIGKILL)
    except OSError:
        pass
    try:
        await asyncio.wait_for(proc.wait(), 5)
    except asyncio.TimeoutError:
        proc.kill()


_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


async def run_text(*cmd: str, cwd: str | None = None, timeout: float = 60) -> str:
    """Run a short CLI command; return its output with ANSI codes stripped.

    Raises RuntimeError with the output when the command fails or times out.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"`{' '.join(cmd[:2])}` timed out after {timeout:.0f}s")
    text = _ANSI.sub("", out.decode("utf-8", "replace")).strip()
    if proc.returncode:
        raise RuntimeError(text or f"exited with code {proc.returncode}")
    return text


_REGISTRY: dict[str, type[AgentProvider]] = {}


def register(name: str):
    def wrap(cls: type[AgentProvider]) -> type[AgentProvider]:
        cls.name = name
        _REGISTRY[name] = cls
        return cls

    return wrap


def get_provider(name: str) -> AgentProvider:
    if name not in _REGISTRY:
        raise KeyError(
            f"unknown provider '{name}', available: {sorted(_REGISTRY)}"
        )
    return _REGISTRY[name]()


def provider_names() -> list[str]:
    return sorted(_REGISTRY)


# Import built-in providers so they self-register.
from . import claude, kimi, muse  # noqa: E402,F401

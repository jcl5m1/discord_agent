"""Claude Code CLI provider: drives `claude -p` (print mode) as a subprocess.

Each Discord message is one `claude -p` invocation with stream-json output;
resuming a previous instance means adding `--resume <id>`. Claude Code's own
session store (under ~/.claude/projects/) is what makes instances survive
harness restarts. Sessions are keyed by working directory, so an instance must
keep its workdir to resume.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import AsyncIterator

from .. import config
from .base import (
    SPAWN_KWARGS, AgentProvider, SessionInfo, StreamEvent, TurnControl, kill_process_tree, register,
    run_text,
)

log = logging.getLogger("discord_agent.claude")

# Emit a warning when the CLI produces no stdout for this long; the turn keeps
# waiting — the warning exists so a hung subprocess is visible in the logs.
_STALL_WARN_INTERVAL = 300.0  # seconds
# After a turn ends with a message unread, how long to wait for Claude to start
# answering it before closing its input.
_UNREAD_GRACE = 20


_SCAN_FILES = 200  # newest transcripts read per listing, so autocomplete stays fast
_SCAN_LINES = 50  # lines read per transcript looking for cwd and first prompt


def _first_prompt(entry: dict) -> str:
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, list):
        content = next((c.get("text") for c in content if isinstance(c, dict) and c.get("type") == "text"), "")
    text = str(content or "")
    # Harness sessions open with the capability brief; the user's prompt follows it.
    text = text.rpartition("[end of harness capabilities]")[2] or text
    return " ".join(text.split())[:80]


def list_sessions(root: Path) -> list[SessionInfo]:
    """Sessions under Claude Code's store: <root>/<sanitized cwd>/<session_id>.jsonl."""
    files = sorted(root.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    found = []
    for path in files[:_SCAN_FILES]:
        cwd = title = ""
        try:
            with path.open(encoding="utf-8", errors="replace") as f:
                for _, line in zip(range(_SCAN_LINES), f):
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    cwd = cwd or entry.get("cwd") or ""
                    if not title and entry.get("type") == "user":
                        title = _first_prompt(entry)
                    if cwd and title:
                        break
            updated = path.stat().st_mtime
        except OSError:
            continue
        if cwd:
            found.append(SessionInfo(id=path.stem, workdir=os.path.normpath(cwd), title=title, updated=updated))
    return found


@register("claude")
class ClaudeProvider(AgentProvider):
    # Short aliases accepted by /new and /model; full model IDs also work.
    models = {
        "opus": "claude-opus-5-5",
        "sonnet": "claude-sonnet-5-5",
        "haiku": "claude-haiku-5-5",
        "fable": "claude-fable-5-1",
    }
    default_model = config.CLAUDE_MODEL

    def sessions(self) -> list[SessionInfo]:
        return list_sessions(Path.home() / ".claude" / "projects")

    async def usage(self) -> str:
        # Print mode answers the built-in /usage itself; no model call is made.
        return await run_text(config.CLAUDE_BIN, "-p", "/usage", cwd=os.path.expanduser("~"))

    supports_live_input = True

    def _build_cmd(self, session_id: str | None, model: str | None) -> list[str]:
        cmd = [config.CLAUDE_BIN]
        if session_id:
            cmd += ["--resume", session_id]
        cmd += ["--model", self.resolve_model(model)]
        if config.CLAUDE_PERMISSION_MODE:
            cmd += ["--permission-mode", config.CLAUDE_PERMISSION_MODE]
        # The prompt goes in on stdin as stream-json, which keeps the turn open for
        # messages sent while it runs and for graceful interrupts (and avoids
        # command-line length and quoting limits). Replayed user messages tell us
        # when Claude has actually read each one.
        cmd += ["-p", "--input-format", "stream-json", "--output-format", "stream-json",
                "--verbose", "--replay-user-messages"]
        return cmd

    @staticmethod
    def _user_line(message_id: str, text: str) -> bytes:
        event = {"type": "user", "uuid": message_id, "message": {"role": "user", "content": text}}
        return (json.dumps(event) + "\n").encode()

    async def run_turn(
        self,
        workdir: str,
        prompt: str,
        session_id: str | None = None,
        model: str | None = None,
        control: TurnControl | None = None,
    ) -> AsyncIterator[StreamEvent]:
        cmd = self._build_cmd(session_id, model)
        os.makedirs(workdir, exist_ok=True)  # agent workdirs are created on demand
        started = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=workdir,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=16 * 1024 * 1024,  # stream-json lines (tool results) can be large
            **SPAWN_KWARGS,
        )
        relay_task: asyncio.Task | None = None
        try:
            log.info(
                "spawned claude pid=%d model=%s session=%s workdir=%s",
                proc.pid, self.resolve_model(model), session_id or "new", workdir,
            )
            final_text_parts: list[str] = []
            captured_session: str | None = None
            announced_session = False
            result_error: str | None = None
            tool_count = 0
            # Messages sent into the turn that Claude hasn't read yet.
            unread: set[str] = control.unread if control is not None else set()
            interrupt_requested = False
            interrupted = False
            # Set when a turn ended with a message still unread: Claude should start
            # another turn for it at once. If it stays silent, its input is closed.
            waiting_since: float | None = None
            assert proc.stdin is not None and proc.stdout is not None
            proc.stdin.write(self._user_line(str(uuid.uuid4()), prompt))
            await proc.stdin.drain()

            def close_stdin() -> None:
                if not proc.stdin.is_closing():
                    proc.stdin.close()  # Claude exits once its input ends

            async def relay() -> None:
                nonlocal interrupt_requested
                while True:
                    kind, payload = await control.requests.get()
                    if proc.stdin.is_closing():
                        continue
                    try:
                        if kind == "message":
                            message_id, _, text = payload.partition("\n")
                            proc.stdin.write(self._user_line(message_id, text))
                        else:
                            interrupt_requested = True
                            proc.stdin.write((json.dumps({
                                "type": "control_request",
                                "request_id": f"interrupt-{uuid.uuid4()}",
                                "request": {"subtype": "interrupt"},
                            }) + "\n").encode())
                        await proc.stdin.drain()
                    except (BrokenPipeError, ConnectionResetError, RuntimeError):
                        return

            if control is not None:
                relay_task = asyncio.get_running_loop().create_task(relay())

            while True:
                try:
                    line = await asyncio.wait_for(
                        proc.stdout.readline(),
                        timeout=_UNREAD_GRACE if waiting_since else _STALL_WARN_INTERVAL,
                    )
                except asyncio.TimeoutError:
                    if waiting_since:
                        log.warning("claude pid=%d didn't pick up %d message(s) after its turn; "
                                    "ending it", proc.pid, len(unread))
                        waiting_since = None
                        close_stdin()
                        continue
                    log.warning(
                        "claude pid=%d silent for %.0fs (elapsed %.0fs, %d tools so far) "
                        "— subprocess has not exited; still waiting",
                        proc.pid, _STALL_WARN_INTERVAL,
                        time.monotonic() - started, tool_count,
                    )
                    continue
                if not line:
                    break
                line = line.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                waiting_since = None
                etype = event.get("type")
                captured_session = event.get("session_id") or captured_session
                if captured_session and not announced_session:
                    announced_session = True
                    yield StreamEvent(kind="session", session_id=captured_session)
                if etype == "user" and event.get("isReplay"):
                    if event.get("uuid") in unread:
                        unread.discard(event["uuid"])
                        yield StreamEvent(kind="delivered", text=event["uuid"])
                elif etype == "assistant" and not event.get("parent_tool_use_id"):
                    for block in (event.get("message") or {}).get("content") or []:
                        if block.get("type") == "text" and block.get("text"):
                            # Separate successive messages (text between tool calls).
                            text = ("\n\n" if final_text_parts else "") + block["text"]
                            final_text_parts.append(text)
                            yield StreamEvent(kind="text", text=text)
                        elif block.get("type") == "tool_use":
                            tool_count += 1
                            name = block.get("name") or "tool"
                            yield StreamEvent(kind="status", text=f"⚙️ tool #{tool_count}: {name}")
                elif etype == "result":
                    if interrupt_requested:
                        interrupted = True
                        close_stdin()
                    elif event.get("is_error"):
                        result_error = str(event.get("result") or event.get("subtype") or "claude error")
                        close_stdin()
                    elif not unread:
                        close_stdin()
                    else:
                        # A message sent near the end hasn't been read yet; Claude
                        # answers it in a further turn on the same process.
                        waiting_since = time.monotonic()

            assert proc.stderr is not None
            stderr_data = await proc.stderr.read()
            returncode = await proc.wait()
            log.info(
                "claude pid=%d exited rc=%d after %.0fs (%d tools, %d reply chars)%s",
                proc.pid, returncode, time.monotonic() - started,
                tool_count, sum(len(p) for p in final_text_parts),
                " (interrupted)" if interrupted else "",
            )
            if stderr_data:
                log.debug("claude pid=%d stderr: %s", proc.pid, stderr_data.decode("utf-8", "replace")[-2000:])

            final_text = "".join(final_text_parts).strip()
            if interrupted:
                yield StreamEvent(kind="interrupted", text=final_text, session_id=captured_session)
            elif returncode != 0 or result_error:
                err = result_error or stderr_data.decode("utf-8", "replace").strip()
                yield StreamEvent(
                    kind="error",
                    text=(err or f"claude exited with code {returncode}")[-1500:],
                    session_id=captured_session,
                )
            else:
                yield StreamEvent(kind="done", text=final_text, session_id=captured_session)
        finally:
            if relay_task is not None:
                relay_task.cancel()
            # Cut short (or the harness gave up on the turn): stop the CLI.
            await kill_process_tree(proc)

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
from typing import AsyncIterator

from .. import config
from .base import AgentProvider, StreamEvent, register

log = logging.getLogger("discord_agent.claude")

# Emit a warning when the CLI produces no stdout for this long; the turn keeps
# waiting — the warning exists so a hung subprocess is visible in the logs.
_STALL_WARN_INTERVAL = 300.0  # seconds


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

    def _build_cmd(self, prompt: str, session_id: str | None, model: str | None) -> list[str]:
        cmd = [config.CLAUDE_BIN]
        if session_id:
            cmd += ["--resume", session_id]
        cmd += ["--model", self.resolve_model(model)]
        if config.CLAUDE_PERMISSION_MODE:
            cmd += ["--permission-mode", config.CLAUDE_PERMISSION_MODE]
        cmd += ["-p", prompt, "--output-format", "stream-json", "--verbose"]
        return cmd

    async def run_turn(
        self, workdir: str, prompt: str, session_id: str | None = None, model: str | None = None
    ) -> AsyncIterator[StreamEvent]:
        cmd = self._build_cmd(prompt, session_id, model)
        os.makedirs(workdir, exist_ok=True)  # agent workdirs are created on demand
        started = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=workdir,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=16 * 1024 * 1024,  # stream-json lines (tool results) can be large
        )
        log.info(
            "spawned claude pid=%d model=%s session=%s workdir=%s",
            proc.pid, self.resolve_model(model), session_id or "new", workdir,
        )
        final_text_parts: list[str] = []
        captured_session: str | None = None
        result_error: str | None = None
        tool_count = 0

        assert proc.stdout is not None
        while True:
            try:
                line = await asyncio.wait_for(
                    proc.stdout.readline(), timeout=_STALL_WARN_INTERVAL
                )
            except asyncio.TimeoutError:
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

            etype = event.get("type")
            captured_session = event.get("session_id") or captured_session
            if etype == "assistant" and not event.get("parent_tool_use_id"):
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
            elif etype == "result" and event.get("is_error"):
                result_error = str(event.get("result") or event.get("subtype") or "claude error")

        assert proc.stderr is not None
        stderr_data = await proc.stderr.read()
        returncode = await proc.wait()
        log.info(
            "claude pid=%d exited rc=%d after %.0fs (%d tools, %d reply chars)",
            proc.pid, returncode, time.monotonic() - started,
            tool_count, sum(len(p) for p in final_text_parts),
        )
        if stderr_data:
            log.debug("claude pid=%d stderr: %s", proc.pid, stderr_data.decode("utf-8", "replace")[-2000:])

        final_text = "".join(final_text_parts).strip()
        if returncode != 0 or result_error:
            err = result_error or stderr_data.decode("utf-8", "replace").strip()
            yield StreamEvent(
                kind="error",
                text=(err or f"claude exited with code {returncode}")[-1500:],
                session_id=captured_session,
            )
        else:
            yield StreamEvent(kind="done", text=final_text, session_id=captured_session)

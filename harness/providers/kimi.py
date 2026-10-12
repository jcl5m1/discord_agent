"""Kimi Code CLI provider: drives `kimi -p` (print mode) as a subprocess.

Each Discord message is one `kimi -p` invocation; resuming a previous instance
means adding `--session <id>`. Kimi's own session store (under
~/.kimi-code/sessions/) is what makes instances survive harness restarts.
"""

from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

from .. import config
from .base import AgentProvider, StreamEvent, register


@register("kimi")
class KimiProvider(AgentProvider):
    def _build_cmd(self, prompt: str, session_id: str | None) -> list[str]:
        cmd = [config.KIMI_BIN]
        if session_id:
            cmd += ["--session", session_id]
        if config.KIMI_MODEL:
            cmd += ["--model", config.KIMI_MODEL]
        cmd += ["-p", prompt, "--output-format", "stream-json"]
        return cmd

    async def run_turn(
        self, workdir: str, prompt: str, session_id: str | None = None
    ) -> AsyncIterator[StreamEvent]:
        cmd = self._build_cmd(prompt, session_id)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=workdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        final_text_parts: list[str] = []
        captured_session: str | None = None
        tool_count = 0

        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            line = line.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            role = event.get("role")
            if role == "assistant":
                text = event.get("content") or ""
                if text:
                    final_text_parts.append(text)
                    yield StreamEvent(kind="text", text=text)
            elif role == "tool":
                tool_count += 1
                name = event.get("name") or "tool"
                yield StreamEvent(kind="status", text=f"⚙️ tool #{tool_count}: {name}")
            elif role == "meta":
                if event.get("type") == "session.resume_hint":
                    captured_session = event.get("session_id") or captured_session

        assert proc.stderr is not None
        stderr_data = await proc.stderr.read()
        returncode = await proc.wait()

        final_text = "".join(final_text_parts).strip()
        if returncode != 0:
            err = stderr_data.decode("utf-8", "replace").strip()
            yield StreamEvent(
                kind="error",
                text=(err or f"kimi exited with code {returncode}")[-1500:],
            )
        else:
            yield StreamEvent(kind="done", text=final_text, session_id=captured_session)

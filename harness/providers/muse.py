"""Muse CLI provider: drives `muse exec --json` (headless mode) as a subprocess.

Each Discord message is one `muse exec` invocation; resuming a previous instance
means passing the session's UUID back via `--session-id`. Muse's own session
store is what makes instances survive harness restarts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import AsyncIterator

from .. import config
from .base import AgentProvider, StreamEvent, register, run_text

log = logging.getLogger("discord_agent.muse")

# Emit a warning when the CLI produces no stdout for this long; the turn keeps
# waiting — the warning exists so a hung subprocess is visible in the logs.
_STALL_WARN_INTERVAL = 300.0  # seconds


@register("muse")
class MuseProvider(AgentProvider):
    # Catalog ids from the muse binary; the "-internal" variants are left out
    # because they are rejected outside Meta ("does not exist or you lack access").
    models = {
        "spark-1.3": "muse-spark-1.3",
        "spark-1.3-contributor": "muse-spark-1.3-contributor",
        "spark-1.2": "muse-spark-1.2",
    }
    default_model = config.MUSE_MODEL

    async def usage(self) -> str:
        return await run_text(config.MUSE_BIN, "usage")

    def _build_cmd(self, prompt: str, session_id: str | None, model: str | None) -> list[str]:
        cmd = [config.MUSE_BIN, "exec", "--json"]
        if session_id:
            cmd += ["--session-id", session_id]
        model = self.resolve_model(model)
        if model:
            cmd += ["--model", model]
        if config.MUSE_EFFORT:
            cmd += ["--reasoning-effort", config.MUSE_EFFORT]
        cmd.append(prompt)
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
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        log.info(
            "spawned muse pid=%d model=%s session=%s workdir=%s",
            proc.pid, self.resolve_model(model) or "cli-default",
            session_id or "new", workdir,
        )
        final_text_parts: list[str] = []
        final_text: str | None = None
        failure: str | None = None
        captured_session: str | None = None
        tool_count = 0

        assert proc.stdout is not None
        while True:
            try:
                line = await asyncio.wait_for(
                    proc.stdout.readline(), timeout=_STALL_WARN_INTERVAL
                )
            except asyncio.TimeoutError:
                log.warning(
                    "muse pid=%d silent for %.0fs (elapsed %.0fs, %d tools so far) "
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

            stream = event.get("stream") or {}
            if stream.get("kind") == "session" and stream.get("id"):
                captured_session = stream["id"]
            ptype = event.get("payload_type") or ""
            payload = event.get("payload") or {}
            if ptype == "run.output.delta":
                text = payload.get("text") or ""
                if text:
                    final_text_parts.append(text)
                    yield StreamEvent(kind="text", text=text)
            elif ptype == "task.lifecycle.side_effect_intent":
                operation = (payload.get("event") or {}).get("operation") or ""
                if operation.startswith("tool:"):
                    tool_count += 1
                    yield StreamEvent(
                        kind="status", text=f"⚙️ tool #{tool_count}: {operation[5:]}"
                    )
            elif ptype.startswith("run.terminal."):
                final_text = payload.get("text")
                if payload.get("terminal") != "completed":
                    failure = payload.get("reason") or payload.get("terminal") or ptype

        assert proc.stderr is not None
        stderr_data = await proc.stderr.read()
        returncode = await proc.wait()
        log.info(
            "muse pid=%d exited rc=%d after %.0fs (%d tools, %d reply chars)",
            proc.pid, returncode, time.monotonic() - started,
            tool_count, sum(len(p) for p in final_text_parts),
        )
        if stderr_data:
            log.debug("muse pid=%d stderr: %s", proc.pid, stderr_data.decode("utf-8", "replace")[-2000:])

        if returncode != 0 or failure:
            err = stderr_data.decode("utf-8", "replace").strip()
            yield StreamEvent(
                kind="error",
                text=(err or str(failure) or f"muse exited with code {returncode}")[-1500:],
            )
        else:
            text = final_text if final_text is not None else "".join(final_text_parts)
            yield StreamEvent(kind="done", text=text.strip(), session_id=captured_session)

"""Kimi Code CLI provider: drives `kimi -p` (print mode) as a subprocess.

Each Discord message is one `kimi -p` invocation; resuming a previous instance
means adding `--session <id>`. Kimi's own session store (under
~/.kimi-code/sessions/) is what makes instances survive harness restarts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import socket
import time
import tomllib
from typing import AsyncIterator

from .. import config
from .base import AgentProvider, StreamEvent, register

log = logging.getLogger("discord_agent.kimi")

# Emit a warning when the CLI produces no stdout for this long; the turn keeps
# waiting — the warning exists so a hung subprocess is visible in the logs.
_STALL_WARN_INTERVAL = 300.0  # seconds


def _load_models() -> dict[str, str]:
    """Short alias -> kimi model key, from the [models.*] tables in config.toml.

    "kimi-code/k3" is offered as "k3"; the full key is what `--model` takes.
    """
    try:
        with open(config.KIMI_CONFIG, "rb") as f:
            keys = tomllib.load(f).get("models", {})
    except (OSError, tomllib.TOMLDecodeError) as e:
        log.warning("could not read kimi models from %s: %s", config.KIMI_CONFIG, e)
        return {}
    return {key.rsplit("/", 1)[-1]: key for key in keys}


_TOKEN_LINE = re.compile(r"Token:\s+(\S+)")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


async def _read_token(proc: asyncio.subprocess.Process) -> str:
    """Read `kimi web` startup output until it prints its bearer token."""
    assert proc.stdout is not None
    seen = []
    while line := await proc.stdout.readline():
        text = _ANSI.sub("", line.decode("utf-8", "replace"))
        seen.append(text)
        if match := _TOKEN_LINE.search(text):
            return match.group(1)
    raise RuntimeError("kimi web exited before printing a token:\n" + "".join(seen)[-800:])


def _format_usage(data: dict) -> str:
    quota = data.get("quota") or {}
    labels = {"limit5h": "5-hour window", "limit7d": "Weekly"}
    lines = []
    for key, entry in (quota.get("usages") or {}).items():
        resets = entry.get("resetAt") or "?"
        pct = 100 * (entry.get("usedRatio") or 0)
        lines.append(f"{labels.get(key, key)}: {pct:.1f}% used, resets {resets}")
    if quota.get("extraUsage"):
        lines.append(f"Extra usage: {quota['extraUsage']}")
    return "\n".join(lines) or f"no quota data ({data.get('kind', 'unknown')})"


@register("kimi")
class KimiProvider(AgentProvider):
    default_model = config.KIMI_MODEL

    @property
    def models(self) -> dict[str, str]:  # re-read so new kimi models show up live
        return _load_models()

    async def usage(self) -> str:
        """Quota from kimi's local web server, the only place the CLI exposes it.

        A throwaway `kimi web` is started on a free loopback port, asked for
        /api/v1/oauth/usage with the bearer token it prints, then stopped.
        """
        import aiohttp

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        proc = await asyncio.create_subprocess_exec(
            config.KIMI_BIN, "web", "--no-open", "--port", str(port),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        try:
            token = await asyncio.wait_for(_read_token(proc), timeout=30)
            async with aiohttp.ClientSession() as http:
                async with http.get(
                    f"http://127.0.0.1:{port}/api/v1/oauth/usage",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    body = await resp.json(content_type=None)
        finally:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    proc.kill()
        if body.get("code") != 0:
            raise RuntimeError(f"kimi usage request failed: {body.get('msg')}")
        return _format_usage(body.get("data") or {})

    def _build_cmd(self, prompt: str, session_id: str | None, model: str | None) -> list[str]:
        cmd = [config.KIMI_BIN]
        if session_id:
            cmd += ["--session", session_id]
        model = self.resolve_model(model)
        if model:
            cmd += ["--model", model]
        cmd += ["-p", prompt, "--output-format", "stream-json"]
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
            "spawned kimi pid=%d model=%s session=%s workdir=%s",
            proc.pid, self.resolve_model(model) or "cli-default",
            session_id or "new", workdir,
        )
        final_text_parts: list[str] = []
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
                    "kimi pid=%d silent for %.0fs (elapsed %.0fs, %d tools so far) "
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
        log.info(
            "kimi pid=%d exited rc=%d after %.0fs (%d tools, %d reply chars)",
            proc.pid, returncode, time.monotonic() - started,
            tool_count, sum(len(p) for p in final_text_parts),
        )
        if stderr_data:
            log.debug("kimi pid=%d stderr: %s", proc.pid, stderr_data.decode("utf-8", "replace")[-2000:])

        final_text = "".join(final_text_parts).strip()
        if returncode != 0:
            err = stderr_data.decode("utf-8", "replace").strip()
            yield StreamEvent(
                kind="error",
                text=(err or f"kimi exited with code {returncode}")[-1500:],
            )
        else:
            yield StreamEvent(kind="done", text=final_text, session_id=captured_session)

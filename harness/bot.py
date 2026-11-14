"""Discord bot: one channel per agent instance, one message per turn.

Messages in a managed channel are forwarded to the instance's provider as a
turn; the reply streams into a single bot message (edited in place), and the
channel topic shows the agent's last message. If the harness (or the whole
machine) restarts, the next message in the channel transparently resumes the
stored provider session.
"""

from __future__ import annotations

import asyncio
import gzip
import io
import json
import logging
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import discord

from . import config, discord_markdown, files, guard, logfile, logins, service, triggers, uptime, workdirs
from .instances import Instance, InstanceManager, normalize_workdir
from .pending import PendingTurn, PendingTurns
from .providers.base import SessionInfo, TurnControl, get_provider, provider_names
from .render import render_html_to_png

if config.PUBLIC_BASE_URL:
    from . import proxy
if config.WHATSAPP_ENABLED:
    from .whatsapp import WhatsAppBridge

logfile.setup("bot")
log = logging.getLogger("discord_agent")

DISCORD_MSG_LIMIT = 2000
TOPIC_LIMIT = 1024
EDIT_INTERVAL = 1.0  # seconds between in-place message edits
# A running turn silent this long is called out as possibly stuck when you message it.
STUCK_AFTER = 600

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

client = discord.Client(intents=intents)


class GuardedCommandTree(discord.app_commands.CommandTree):
    """Refuse commands in channels that belong to another bot on this server."""

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        channel = client.get_channel(interaction.channel_id) if interaction.channel_id else None
        if channel is None:  # DMs, or not in the cache: nothing to judge it by
            return True
        if _channel_allowed(channel):
            return True
        log.info("refused /%s in #%s: not this bot's channel",
                 getattr(interaction.command, "name", "?"), getattr(channel, "name", "?"))
        # Autocomplete requests run through here too, and can't take a message.
        if interaction.type is discord.InteractionType.application_command:
            await interaction.response.send_message(
                f"⛔ #{channel.name} belongs to another bot, so **{client.user.name}** isn't "
                f"taking commands here. Use that bot's command, or run it in "
                f"{' / '.join('#' + c for c in sorted(config.SHARED_CHANNELS))}.",
                ephemeral=True,
            )
        return False


tree = GuardedCommandTree(client)

store = InstanceManager()
# WhatsApp mirror; None when WHATSAPP_OWNER is unset. Created in setup_hook.
wa: "WhatsAppBridge | None" = None

# User messages waiting for (or in) a turn, so edits and deletes reach the agent.
pending = PendingTurns()

# Serialize turns within a channel; cap total concurrent turns globally.
# Take the channel lock BEFORE the global slot, so messages queued behind a
# busy channel don't hold global slots and starve other channels.
_channel_locks: dict[int, asyncio.Lock] = {}
_global_semaphore = asyncio.Semaphore(config.MAX_CONCURRENT_TURNS)

# Turns currently running, keyed by channel id. Feeds the in-channel progress
# updates and the heartbeat file the watchdog process monitors.
_active_turns: dict[int, dict] = {}
# Most recent channel with user activity; the watchdog's fallback alert target.
_last_channel_id: int | None = None
# Written by /update just before restarting; the next startup reports back.
UPDATE_MARKER = config.DATA_DIR / "update_pending.json"


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 120:
        return f"{seconds}s"
    if seconds < 7200:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


@client.event
async def setup_hook() -> None:
    global wa
    if config.PUBLIC_BASE_URL:
        await proxy.start_proxy(store, fire_trigger)
    if config.WHATSAPP_ENABLED:
        wa = WhatsAppBridge(store, _process_whatsapp_message)
        try:
            await wa.start()
        except Exception:
            log.exception("whatsapp bridge failed to start; continuing Discord-only")
            wa = None
    asyncio.get_running_loop().create_task(_heartbeat_loop())


def _wa_task(coro) -> None:
    """Fire-and-forget a WhatsApp send so mirroring never delays Discord."""
    asyncio.get_running_loop().create_task(coro)


async def _heartbeat_loop() -> None:
    """Rewrite data/heartbeat.json so the watchdog can detect a crash or wedge."""
    while True:
        try:
            payload = {
                "ts": time.time(),
                "pid": os.getpid(),
                "last_channel_id": _last_channel_id,
                "turns": [
                    {
                        "channel_id": channel_id,
                        "instance": t["instance"],
                        "model": t["model"],
                        "started_at": t["started_at"],
                        "last_event_at": t["last_event_at"],
                        "tools": t["tools"],
                    }
                    for channel_id, t in _active_turns.items()
                ],
            }
            tmp = config.HEARTBEAT_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload))
            tmp.replace(config.HEARTBEAT_PATH)
        except Exception:
            log.exception("heartbeat write failed")
        await asyncio.sleep(config.HEARTBEAT_INTERVAL)


async def _progress_updates(inst: Instance, channel: discord.abc.Messageable, turn: dict) -> None:
    """Post a short 'still working' status while a turn runs long."""
    while True:
        await asyncio.sleep(config.STATUS_UPDATE_INTERVAL)
        elapsed = _fmt_duration(time.time() - turn["started_at"])
        silent = _fmt_duration(time.time() - turn["last_event_at"])
        log.info("[%s] still running: %s elapsed, %d tools, last output %s ago",
                 inst.name, elapsed, turn["tools"], silent)
        note = (
            f"⏳ **{inst.name}** still working — {elapsed} elapsed, "
            f"{turn['tools']} tool calls, last output {silent} ago "
            f"(model `{turn['model']}`)"
        )
        if wa:
            _wa_task(wa.send_text(inst, note))
        try:
            await channel.send(note)
        except discord.HTTPException:
            log.warning("[%s] progress update failed to send", inst.name)


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9-_]", "-", name.lower()).strip("-")
    return slug or "agent"


_HTML_BLOCK_RE = re.compile(r"```html\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


def _extract_html_block(text: str) -> str | None:
    m = _HTML_BLOCK_RE.search(text)
    return m.group(1).strip() if m else None


def _strip_html_block(text: str) -> str:
    return _HTML_BLOCK_RE.sub("", text).strip()


def _channel_lock(channel_id: int) -> asyncio.Lock:
    if channel_id not in _channel_locks:
        _channel_locks[channel_id] = asyncio.Lock()
    return _channel_locks[channel_id]


def _listening_ports() -> set[int]:
    """TCP ports currently listening on this machine (user-accessible ones)."""
    windows = os.name == "nt"
    cmd = ["netstat", "-ano", "-p", "tcp"] if windows else ["ss", "-tlnH"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return set()
    ports = set()
    for line in out.splitlines():
        parts = line.split()
        if windows:
            if len(parts) < 5 or parts[3] != "LISTENING":
                continue
            local_address = parts[1]
        else:
            if len(parts) < 4:
                continue
            local_address = parts[3]
        tail = local_address.rsplit(":", 1)[-1]
        if tail.isdigit():
            port = int(tail)
            if 1024 <= port <= 65535 and port != config.PROXY_PORT:
                ports.add(port)
    return ports


# Ports we have already posted a link for, per instance name.
_announced_ports: dict[str, set[int]] = {}


def _new_server_urls(inst: Instance, before: set[int], after: set[int]) -> list[str]:
    """Build public URLs for servers that started listening during the turn."""
    if not config.PUBLIC_BASE_URL:
        return []
    fresh = after - before - _announced_ports.setdefault(inst.name, set())
    fresh = {p for p in fresh if config.AGENT_PORT_START <= p <= config.AGENT_PORT_END}
    token = inst.ensure_web_token()
    store.put(inst)
    urls = [
        f"{config.PUBLIC_BASE_URL}/agents/{_slug(inst.name)}/{token}/{port}/"
        for port in sorted(fresh)
    ]
    _announced_ports[inst.name] |= fresh
    return urls


# Injected as a prefix on the FIRST turn of every session; the session then
# carries it forward, so users never have to spell out serving instructions.
_HARNESS_BRIEF = """\
[harness capabilities — persistent for this session]
You are running inside a Discord agent harness on a {host_platform} host.
Working directory: {workdir}
Output formats you can choose from, in order of richness:
1. Live web page — for interactive or visual output (demos, dashboards, forms,
   games, visualizations). Build the page, then serve it in the background on a
   port in the range {port_start}-{port_end}, detached so it outlives your turn:
       {server_command}
   (or any small server). The harness detects the server and posts a public URL
   in the Discord channel automatically — you do not need to construct URLs.
   Use this format whenever the result benefits from being interactive.
2. Static HTML in a fenced ```html code block — the harness renders it to an
   image. Good for one-shot rich documents (tables, reports, layouts).
3. Plain Markdown text — for everything else. Discord can't display Markdown
   tables, so the harness turns them into lists; prefer lists or short
   paragraphs. Headings below ### show as bold text.
Keep servers on ports {port_start}-{port_end} only; other ports are ignored.
Annotation feedback (already wired up — nothing for you to enable): every HTML
page you serve is automatically injected with the reviewjs annotate toolbar.
The user can highlight text, draw, pin, and comment directly on the page;
comments autosave server-side and follow them across devices. All comments for
this instance accumulate in ONE file: {annotations_path}
At the start of ANY turn that revises, updates, or follows up on a served page,
read that file first — it is self-describing JSON whose comment entries carry
'text' (the feedback), 'page', 'type', and 'resolved' fields. Treat unresolved
comments as the user's change requests. After applying feedback you may tell
the user they can mark comments resolved in the page UI.
Shared files for this channel: {files_dir}
Save charts, images, logs, reports and data you want to show or keep for this
discussion there (organise by topic in subfolders, e.g. charts/, logs/, data/).
Everything in it is served read-only at {files_url}<path> -- a stable,
unguessable public link; image links posted in your reply display inline in
Discord, and the folder URL itself is a browsable index. The user can list them
with /files. Prefer this over ad-hoc share folders or temporary servers for
static files.
Sessions are managed by the harness: never launch the agent CLI yourself to
resume or continue another session (e.g. `kimi --session`, `claude --resume`) --
it can't attach to this channel and fails if that session is open elsewhere.
If the user wants to continue a different session, tell them to run /session
in this Discord channel and pick it there.
[end of harness capabilities]
"""


def _with_brief(inst: Instance, prompt: str) -> str:
    """Prepend the capability brief when this turn starts a new session."""
    if inst.session_id is not None:
        return prompt
    folder = files.files_dir(_slug(inst.name))
    folder.mkdir(parents=True, exist_ok=True)
    token = inst.ensure_web_token()
    store.put(inst)
    return _HARNESS_BRIEF.format(
        host_platform="Windows" if os.name == "nt" else "Linux",
        server_command=(
            "Start-Process python -ArgumentList '-m','http.server','<port>' "
            "-WorkingDirectory <dir> -WindowStyle Hidden"
            if os.name == "nt" else
            "cd <dir> && setsid nohup python3 -m http.server <port> >/dev/null 2>&1 &"
        ),
        workdir=inst.workdir,
        port_start=config.AGENT_PORT_START,
        port_end=config.AGENT_PORT_END,
        annotations_path=config.DATA_DIR / "annotations" / f"{_slug(inst.name)}.json",
        files_dir=folder,
        files_url=files.files_url(_slug(inst.name), token) or "(not served: PUBLIC_BASE_URL unset) ",
    ) + "\n" + prompt


async def _run_turn(inst: Instance, prompt: str, status_msg: discord.Message) -> str:
    """Run one provider turn for an instance, streaming into status_msg.

    Returns how it ended: "done", "error", "interrupted", or "crashed" (the CLI
    failed before writing anything or using a tool; see _run_turn_recovering).
    """
    provider = get_provider(inst.provider)
    loop = asyncio.get_running_loop()
    started = loop.time()
    model_label = _model_label(inst)
    log.info(
        "[%s] turn start: provider=%s model=%s session=%s workdir=%s prompt=%.160s",
        inst.name, inst.provider, model_label,
        inst.session_id or "new", inst.workdir, prompt,
    )
    ports_before = _listening_ports()

    buf = ""
    last_edit = 0.0
    error: str | None = None
    turn = {
        "instance": inst.name,
        "model": model_label,
        "started_at": time.time(),
        "last_event_at": time.time(),
        "tools": 0,
        # Set by /interrupt (or a "stop" message): ends the turn, keeps the session.
        "interrupt": asyncio.Event(),
        "interrupted_by": None,
        # For agents that stop gracefully: how /interrupt reaches the turn. For
        # agents that also take live input: messages sent into the running
        # turn, and the Discord message each came from (to mark it once read).
        "control": TurnControl() if provider.supports_graceful_interrupt else None,
        "live_input": provider.supports_live_input,
        "steered": {},
    }
    _active_turns[inst.channel_id] = turn
    progress = asyncio.get_running_loop().create_task(
        _progress_updates(inst, status_msg.channel, turn)
    )
    stop_view = StopTurnView(inst, turn)
    try:
        await status_msg.edit(view=stop_view)
    except discord.HTTPException:
        log.warning("[%s] couldn't add the stop button", inst.name)
    if wa:
        _wa_task(wa.typing(inst))

    def display(text: str) -> str:
        text = discord_markdown.adapt(text.strip())
        if len(text) <= DISCORD_MSG_LIMIT:
            return text
        # First chunk cuts at a line break and closes any open code block.
        return discord_markdown.split_message(text, DISCORD_MSG_LIMIT - 20)[0] + "\n…(truncated)"

    def capture_session(session_id: str | None) -> None:
        if (
            session_id
            and session_id != inst.session_id
            and store.get(inst.channel_id) is inst  # not /delete'd mid-turn
        ):
            inst.session_id = session_id
            store.put(inst)
            log.info("[%s] session id captured: %s", inst.name, inst.session_id)

    interrupted = False
    interrupt_wait = asyncio.ensure_future(turn["interrupt"].wait())
    try:
        try:
            extra = {"control": turn["control"]} if turn["control"] else {}
            agen = provider.run_turn(
                inst.workdir, _with_brief(inst, prompt), inst.session_id, inst.model, **extra
            )
            while True:
                step = asyncio.ensure_future(agen.__anext__())
                await asyncio.wait({step, interrupt_wait}, return_when=asyncio.FIRST_COMPLETED)
                if not step.done():
                    # Cancelling the provider mid-await runs its cleanup, which
                    # kills the CLI and whatever it started.
                    step.cancel()
                    try:
                        await step
                    except (asyncio.CancelledError, StopAsyncIteration):
                        pass
                    await agen.aclose()
                    interrupted = True
                    break
                try:
                    event = step.result()
                except StopAsyncIteration:
                    break
                turn["last_event_at"] = time.time()
                if event.kind == "text":
                    buf += event.text
                elif event.kind == "status":
                    turn["tools"] += 1
                    log.info("[%s] %s", inst.name, event.text)
                elif event.kind == "session":
                    capture_session(event.session_id)
                elif event.kind == "delivered":
                    if (msg := turn["steered"].pop(event.text, None)) is not None:
                        _wa_task(_react(msg, "✅"))
                elif event.kind == "interrupted":
                    interrupted = True
                    buf = event.text or buf
                    capture_session(event.session_id)
                elif event.kind == "error":
                    error = event.text
                elif event.kind == "done":
                    buf = event.text or buf
                    capture_session(event.session_id)

                now = loop.time()
                if buf and now - last_edit >= EDIT_INTERVAL:
                    last_edit = now
                    await status_msg.edit(content=display(buf) or "…")
        except Exception:
            log.exception("turn failed for %s", inst.name)
            error = "harness error while running the agent (see logs)"

        if interrupted:
            who = turn["interrupted_by"] or "you"
            log.info("[%s] turn interrupted by %s after %.0fs", inst.name, who, loop.time() - started)
            note = f"⏹️ Interrupted by {who}. The session is kept, so the agent remembers this work."
            body = display(buf) if buf.strip() else ""
            text = f"{body}\n\n{note}" if body else note
            if len(text) > DISCORD_MSG_LIMIT:
                text = discord_markdown.split_message(body, DISCORD_MSG_LIMIT - len(note) - 30)[0] + f"\n…\n\n{note}"
            await status_msg.edit(content=text, view=None)
            if wa:
                _wa_task(wa.send_text(inst, note))
            return "interrupted"

        if error:
            log.warning(
                "[%s] turn failed after %.0fs: %.300s",
                inst.name, loop.time() - started, error,
            )
            crashed = (
                not buf.strip()
                and turn["tools"] == 0
                and not error.startswith("harness error")
                and not _SESSION_IN_USE_RE.search(error)
            )
            if crashed:
                await status_msg.edit(
                    content=display(f"⚠️ The agent's session crashed before replying: {error}"),
                    view=None,
                )
                return "crashed"
            if inst.session_id and _SESSION_IN_USE_RE.search(error):
                await status_msg.edit(
                    content=display(
                        f"❌ {error}\n\nAnother `{inst.provider}` process still has this session "
                        "open, maybe a turn from this instance's old channel or a terminal on "
                        "this machine. Start a fresh session, or retry once it's closed."
                    ),
                    view=SessionInUseView(inst, prompt),
                )
            else:
                await status_msg.edit(content=f"❌ {display(error)}", view=None)
            if wa:
                _wa_task(wa.send_text(inst, f"❌ {error}"))
            return "error"

        log.info(
            "[%s] turn done in %.0fs, %d chars of reply",
            inst.name, loop.time() - started, len(buf.strip()),
        )
        final_text = buf.strip() or "(no reply)"

        # Live web servers: if the turn started listening on new ports, post links.
        urls = _new_server_urls(inst, ports_before, _listening_ports())
        if urls:
            final_text += "\n\n🌐 live:" + "".join(f"\n<{u}>" for u in urls)

        # Rich output: if the reply contains a fenced ```html block, screenshot it
        # and attach the PNG (Discord can't render HTML inline).
        image: discord.File | None = None
        source: discord.File | None = None
        png: bytes | None = None
        html_block = _extract_html_block(final_text)
        if html_block:
            try:
                png = await render_html_to_png(html_block)
                image = discord.File(io.BytesIO(png), filename=f"{_slug(inst.name)}-output.png")
                source = discord.File(
                    io.BytesIO(html_block.encode()), filename=f"{_slug(inst.name)}-output.html"
                )
                final_text = _strip_html_block(final_text) + "\n\n📎 rendered above · HTML source attached"
            except Exception:
                log.exception("HTML render failed for %s; falling back to text", inst.name)
                png = None
                final_text += "\n\n(html rendering failed — see raw source above)"

        # Tables and deep headings don't render in Discord (or WhatsApp).
        final_text = discord_markdown.adapt(final_text)

        if wa:
            _wa_task(_mirror_reply(inst, final_text, png, html_block))

        # Split over the limit at line breaks, keeping code blocks intact.
        chunks = discord_markdown.split_message(final_text, DISCORD_MSG_LIMIT)
        if image:
            await status_msg.edit(content=chunks[0], attachments=[image], view=None)
        else:
            await status_msg.edit(content=chunks[0], view=None)
        for chunk in chunks[1:]:
            await status_msg.channel.send(chunk)
        if source:
            await status_msg.channel.send(file=source)

        topic = final_text[:TOPIC_LIMIT]
        try:
            await status_msg.channel.edit(topic=topic)
        except discord.Forbidden:
            log.warning("missing permission to set channel topic for %s", inst.name)
        return "done"
    finally:
        stop_view.stop()
        interrupt_wait.cancel()
        _active_turns.pop(inst.channel_id, None)
        progress.cancel()
        # Messages sent into the turn that the agent never read: after an
        # interrupt they're dropped like the rest of the queue; otherwise they
        # get a turn of their own.
        for msg in turn["steered"].values():
            if interrupted:
                log.info("[%s] message %s sent into the turn was dropped by the interrupt",
                         inst.name, msg.id)
            elif store.get(inst.channel_id) is inst:
                log.info("[%s] message %s wasn't read before the turn ended; queued", inst.name, msg.id)
                asyncio.get_running_loop().create_task(_process_user_message(inst, msg))


@client.event
async def on_ready() -> None:
    guild = discord.Object(id=config.GUILD_ID)
    tree.copy_global_to(guild=guild)
    await tree.sync(guild=guild)
    log.info("logged in as %s", client.user)
    if config.IGNORED_API_KEYS:
        log.warning("ignoring agent API keys (subscription logins are used instead): %s",
                    ", ".join(config.IGNORED_API_KEYS))
    asyncio.get_running_loop().create_task(_announce_update())
    for inst in store.all():
        asyncio.get_running_loop().create_task(_catch_up(inst))


def _channel_allowed(channel) -> bool:
    return guard.channel_allowed(
        channel,
        category_id=config.CATEGORY_ID,
        shared=config.SHARED_CHANNELS,
        instance_channel_ids={i.channel_id for i in store.all()},
    )


@client.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot or not message.guild:
        return
    inst = store.get(message.channel.id)
    if inst is None or inst.stopped:
        return
    log.info(
        "[%s] message from %s in #%s: %.160s",
        inst.name, message.author, message.channel.name, message.content,
    )
    if _is_stop_word(message.content) and _interrupt(inst, message.author.display_name, drop_queue=True):
        await _react(message, "⏹️")
        return
    if (redirect := _redirect_text(message.content)) and _interrupt(
        inst, message.author.display_name, drop_queue=True
    ):
        await _react(message, "⏹️")
        turn = pending.add(message.id, redirect, channel_id=message.channel.id)
        await _process_user_message(inst, message, turn)
        return
    if _steer(inst, message):
        if notice := _busy_notice(inst, steered=True):
            await message.channel.send(notice)
        if wa:
            _wa_task(wa.send_text(inst, f"💬 *{message.author.display_name}* (Discord):\n{message.content}"))
        return
    await _process_user_message(inst, message)


async def _hard_stop_after(inst: Instance, turn: dict, grace: float) -> None:
    """Kill the CLI if a graceful interrupt hasn't ended the turn in time."""
    await asyncio.sleep(grace)
    if _active_turns.get(inst.channel_id) is turn and not turn["interrupt"].is_set():
        log.warning("[%s] still running %.0fs after the interrupt; killing it", inst.name, grace)
        turn["interrupt"].set()


def _steer(inst: Instance, message: discord.Message, text: str | None = None) -> bool:
    """Send a message straight into the running turn, if the agent can take it.

    Returns False when it should be queued as a turn of its own instead.
    """
    turn = _active_turns.get(inst.channel_id)
    if (
        not config.STEER_RUNNING_TURNS
        or turn is None
        or turn["control"] is None
        or not turn["live_input"]
        or turn["interrupted_by"]
        or turn["interrupt"].is_set()
    ):
        return False
    text = text if text is not None else message.content
    turn["steered"][turn["control"].send(text)] = message
    pending.remember(message.id, text)
    log.info("[%s] message %s sent into the running turn", inst.name, message.id)
    _wa_task(_react(message, "⏩"))
    return True


# A message that's only one of these, sent while a turn runs, interrupts it.
_STOP_WORDS = {"stop", "!stop", "⏹️", "⏹"}


def _redirect_text(text: str) -> str | None:
    """For "! new instructions": the instructions; None for anything else.

    Sent while a turn runs, such a message interrupts it and becomes the next turn.
    """
    text = text.strip()
    if not text.startswith("!") or _is_stop_word(text):
        return None
    return text[1:].strip() or None


def _is_stop_word(text: str) -> bool:
    return text.strip().lower().rstrip(".!") in _STOP_WORDS or text.strip() in _STOP_WORDS


def _interrupt(inst: Instance, by: str, *, drop_queue: bool) -> tuple[bool, int] | None:
    """Stop the turn running in the instance's channel; returns (stopped, dropped).

    None when no turn is running, so a plain "stop" with nothing to stop is sent
    to the agent as an ordinary message.
    """
    turn = _active_turns.get(inst.channel_id)
    if turn is None or turn["interrupt"].is_set() or turn["interrupted_by"]:
        return None
    turn["interrupted_by"] = by
    if turn["control"] is not None:
        # Like pressing Esc in the CLI: the agent stops at once and stays usable.
        turn["control"].interrupt()
        asyncio.get_running_loop().create_task(_hard_stop_after(inst, turn, config.INTERRUPT_GRACE))
    else:
        turn["interrupt"].set()  # no live input: the CLI is killed
    dropped = pending.drop_channel(inst.channel_id) if drop_queue else 0
    log.info("[%s] interrupted by %s (%d queued message(s) dropped)", inst.name, by, dropped)
    return True, dropped


async def _mirror_reply(
    inst: Instance, text: str, png: bytes | None, html_block: str | None
) -> None:
    """Echo a finished agent reply into the instance's WhatsApp group, in order."""
    assert wa is not None
    await wa.typing(inst, False)
    await wa.send_text(inst, text)
    if png:
        await wa.send_image(inst, png, caption=f"{inst.name} output")
        await wa.send_document(
            inst, html_block.encode(), f"{_slug(inst.name)}-output.html", "text/html"
        )


async def _react(message: discord.Message, emoji: str) -> None:
    try:
        await message.add_reaction(emoji)
    except discord.NotFound:  # deleted in the meantime
        pass


async def _process_user_message(
    inst: Instance, message: discord.Message, turn: PendingTurn | None = None
) -> None:
    """Run a turn for a Discord message (or for an edit of one, when turn is given).

    Registered before the first await, so an edit or delete that arrives while
    the message waits for the channel is applied to it.
    """
    global _last_channel_id
    turn = turn or pending.add(message.id, message.content, channel_id=message.channel.id)
    _last_channel_id = message.channel.id
    if notice := _busy_notice(inst, steered=False):
        await message.channel.send(notice)
    if wa:
        verb = "edited" if turn.edit_of is not None else "(Discord)"
        _wa_task(wa.send_text(inst, f"💬 *{message.author.display_name}* {verb}:\n{turn.content}"))
    try:
        async with _channel_lock(message.channel.id):
            async with _global_semaphore:
                if turn.deleted:
                    log.info("[%s] message %s deleted before its turn; skipped", inst.name, message.id)
                    return
                turn.started = True
                async with message.channel.typing():
                    status_msg = await message.channel.send("🤔 …")
                    await _run_turn_recovering(inst, turn.prompt(), status_msg)
        if store.get(message.channel.id) is inst:  # not /delete'd mid-turn
            inst.last_message_id = str(message.id)
            store.put(inst)
        await _react(message, "✅")
    except Exception:
        log.exception("message turn failed for %s", inst.name)
        await _react(message, "❌")
    finally:
        pending.finish(turn)


@client.event
async def on_raw_message_edit(payload: discord.RawMessageUpdateEvent) -> None:
    data = payload.data
    if "content" not in data or (data.get("author") or {}).get("bot"):
        return
    inst = store.get(payload.channel_id)
    if inst is None or inst.stopped:
        return
    outcome, turn = pending.edit(payload.message_id, data["content"], channel_id=payload.channel_id)
    if outcome == "updated":
        log.info("[%s] queued message %s edited before its turn", inst.name, payload.message_id)
        await _react(payload.message, "✏️")
    elif outcome == "followup":
        log.info("[%s] message %s edited after the agent saw it; follow-up queued",
                 inst.name, payload.message_id)
        asyncio.get_running_loop().create_task(
            _process_user_message(inst, payload.message, turn)
        )


def _drop_deleted(channel_id: int, message_ids) -> None:
    inst = store.get(channel_id)
    if inst is None:
        return
    for message_id in message_ids:
        if pending.delete(message_id):
            log.info("[%s] message %s deleted while queued; dropped", inst.name, message_id)
            if wa:
                _wa_task(wa.send_text(inst, "🗑️ a queued Discord message was deleted before the agent saw it"))


@client.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent) -> None:
    _drop_deleted(payload.channel_id, [payload.message_id])


@client.event
async def on_raw_bulk_message_delete(payload: discord.RawBulkMessageDeleteEvent) -> None:
    _drop_deleted(payload.channel_id, payload.message_ids)


async def _process_whatsapp_message(inst: Instance, text: str, sender: str) -> None:
    """A turn typed in the instance's WhatsApp group: echo to Discord, then run it."""
    global _last_channel_id
    assert wa is not None
    # WhatsApp connects (and replays queued messages) before Discord is ready.
    await client.wait_until_ready()
    if inst.stopped:
        await wa.send_text(inst, f"⏸️ {inst.name} is stopped — `/resume name:{inst.name}` in Discord.")
        return
    channel = client.get_channel(inst.channel_id)
    if not isinstance(channel, discord.TextChannel):
        await wa.send_text(inst, f"⚠️ {inst.name}'s Discord channel is gone — `/resume` it there first.")
        return
    if _is_stop_word(text) and _interrupt(inst, sender, drop_queue=True):
        await channel.send(f"⏹️ **{sender}** interrupted the agent from WhatsApp.")
        return
    if (redirect := _redirect_text(text)) and _interrupt(inst, sender, drop_queue=True):
        await channel.send(f"⏹️ **{sender}** interrupted the agent from WhatsApp.")
        text = redirect
    _last_channel_id = channel.id
    echo = f"📱 **{sender}** (WhatsApp):\n{text}"
    try:
        mirror = await channel.send(echo[:DISCORD_MSG_LIMIT])
    except discord.HTTPException:
        log.exception("[%s] could not mirror whatsapp message to Discord", inst.name)
        mirror = None
    if mirror is not None and _steer(inst, mirror, text):
        return
    if notice := _busy_notice(inst, steered=False):
        await channel.send(notice)
    try:
        async with _channel_lock(channel.id):
            async with _global_semaphore:
                async with channel.typing():
                    status_msg = await channel.send("🤔 …")
                    await _run_turn_recovering(inst, text, status_msg)
        if mirror:
            await mirror.add_reaction("✅")
    except Exception:
        log.exception("whatsapp turn failed for %s", inst.name)
        if mirror:
            await mirror.add_reaction("❌")
        await wa.send_text(inst, "❌ harness error while running the agent (see logs)")


async def _catch_up(inst: Instance) -> None:
    """Process user messages that arrived while the harness was offline.

    Uses the per-channel last-processed message id as a persistent marker, so
    a restart after a crash or a long downtime still answers everything sent
    to managed channels in the meantime.
    """
    if not inst.last_message_id or inst.stopped:
        return
    channel = client.get_channel(inst.channel_id)
    if not isinstance(channel, discord.TextChannel):
        log.warning("catch-up: channel %s for %s not in cache", inst.channel_id, inst.name)
        return
    log.info("catching up in #%s since message %s", channel.name, inst.last_message_id)
    try:
        async for msg in channel.history(
            after=discord.Object(id=int(inst.last_message_id)), oldest_first=True
        ):
            if msg.type is not discord.MessageType.default or msg.author.bot:
                continue
            await _process_user_message(inst, msg)
    except discord.Forbidden:
        log.warning(
            "catch-up skipped for %s: bot lacks 'Read Message History' in that channel — "
            "grant it to the bot's role (Server Settings → Roles → bot role)",
            inst.name,
        )
    except Exception:
        log.exception("catch-up failed for %s", inst.name)


def _model_label(inst: Instance) -> str:
    return get_provider(inst.provider).resolve_model(inst.model) or "CLI default"


async def _workdir_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[discord.app_commands.Choice[str]]:
    """Browse folders one level at a time, starting from the home directory."""
    recent = sorted({i.workdir for i in store.all()})
    return [
        discord.app_commands.Choice(name=name, value=value)
        for name, value in workdirs.suggest(current, Path.home(), recent)
    ]


async def _model_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[discord.app_commands.Choice[str]]:
    """Suggest "provider:alias" for every provider's models.

    The current channel's provider is listed first; a provider already picked in
    the command narrows the list to that provider.
    """
    inst = store.get(interaction.channel_id) if interaction.channel_id else None
    picked = getattr(interaction.namespace, "provider", None)
    first = picked or (inst.provider if inst else config.DEFAULT_PROVIDER)
    names = [picked] if picked else sorted(provider_names(), key=lambda p: p != first)
    needle = current.lower()
    choices = []
    for pname in names:
        for alias, model_id in get_provider(pname).models.items():
            value = f"{pname}:{alias}"
            if needle in value.lower() or needle in model_id.lower():
                choices.append(
                    discord.app_commands.Choice(name=f"{value} ({model_id})", value=value)
                )
    return choices[:25]


def _split_model(provider: str | None, model: str | None) -> tuple[str | None, str | None]:
    """Split a "provider:model" autocomplete value into its parts.

    A bare model is returned unchanged. Raises ValueError when the prefix
    contradicts an explicitly chosen provider.
    """
    if model and ":" in model:
        prefix, rest = model.split(":", 1)
        if prefix in provider_names():
            if provider and provider != prefix:
                raise ValueError(
                    f"model `{model}` belongs to `{prefix}`, but provider `{provider}` was chosen"
                )
            return prefix, rest or None
    return provider, model


@tree.command(name="new", description="Create a new agent instance channel")
@discord.app_commands.describe(
    name="Instance name (becomes the channel name)",
    workdir="Folder to work in: pick folders one level at a time (default: DEFAULT_WORKDIR)",
    prompt="Optional first prompt to start the agent with",
    provider="Agent CLI to use (default: DEFAULT_PROVIDER)",
    model="provider:model from the list, or a bare alias/ID (default: the provider's default)",
)
@discord.app_commands.choices(
    provider=[discord.app_commands.Choice(name=p, value=p) for p in provider_names()]
)
@discord.app_commands.autocomplete(model=_model_autocomplete, workdir=_workdir_autocomplete)
async def cmd_new(
    interaction: discord.Interaction,
    name: str,
    workdir: str | None = None,
    prompt: str | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> None:
    try:
        provider, model = _split_model(provider, model)
    except ValueError as e:
        await interaction.response.send_message(f"❌ {e}")
        return
    await interaction.response.defer()

    guild = interaction.guild
    assert guild is not None

    existing = store.by_name(name)
    if existing is not None:
        options = dict(provider=provider, model=model, workdir=workdir, prompt=prompt)
        switching = provider is not None and provider != existing.provider
        if existing.session_id is None or switching:
            # Nothing to resume (session ids are provider-specific): just reopen.
            channel, msg = await _reopen_instance(guild, existing, fresh=True, **options)
            await interaction.followup.send(msg)
            if prompt:
                await on_message_simple(channel, prompt)
            return
        view = ReopenView(existing, owner_id=interaction.user.id, **options)
        view.message = await interaction.followup.send(view.question(), view=view, wait=True)
        return

    channel = await guild.create_text_channel(
        name=_slug(name), category=discord.Object(id=config.CATEGORY_ID)
    )
    inst = Instance(
        channel_id=channel.id,
        name=name,
        provider=provider or config.DEFAULT_PROVIDER,
        workdir=workdir or config.DEFAULT_WORKDIR,
        model=model,
    )
    inst.ensure_web_token()
    store.put(inst)
    if wa:
        await wa.ensure_group(inst)  # before the first prompt, so its reply is mirrored
    await interaction.followup.send(
        f"✅ created {channel.mention} — just chat in it. "
        f"Provider: `{inst.provider}`, model: `{_model_label(inst)}`, workdir: `{inst.workdir}`"
    )
    if prompt:
        await on_message_simple(channel, prompt)


def _instance_turn_running(inst: Instance) -> bool:
    """A turn for this instance is running, possibly in a channel it has since left."""
    return any(t["instance"] == inst.name for t in _active_turns.values())


async def _reopen_instance(
    guild: discord.Guild,
    inst: Instance,
    *,
    fresh: bool,
    provider: str | None = None,
    model: str | None = None,
    workdir: str | None = None,
    prompt: str | None = None,
) -> tuple[discord.TextChannel, str]:
    """Give an existing instance a new channel, resuming its session or starting fresh."""
    channel = await guild.create_text_channel(
        name=_slug(inst.name), category=discord.Object(id=config.CATEGORY_ID)
    )
    inst.channel_id = channel.id
    inst.stopped = False
    if provider and provider != inst.provider:
        inst.provider = provider
        inst.model = model
        fresh = True  # session ids are provider-specific
    elif model:
        inst.model = model
    note = ""
    if fresh:
        inst.session_id = None
        if workdir:
            inst.workdir = normalize_workdir(workdir)
    elif workdir:
        note = " The folder is unchanged: a resumed session keeps its own."
    store.put(inst)
    if wa:
        _wa_task(wa.ensure_group(inst))
    how = (f"resuming `{inst.provider}` session `{inst.session_id}`" if inst.session_id
           else f"fresh `{inst.provider}` session")
    log.info("[%s] reopened in #%s: %s", inst.name, channel.name, how)
    return channel, (
        f"✅ reopened {channel.mention}: {how} on the next message "
        f"(model `{_model_label(inst)}`, workdir `{inst.workdir}`).{note}"
    )


class ReopenView(discord.ui.View):
    """Resume / Start fresh / Cancel, for /new with a name that already exists."""

    def __init__(self, inst: Instance, *, owner_id: int, **options):
        super().__init__(timeout=600)
        self.inst = inst
        self.owner_id = owner_id
        self.options = options
        self.message: discord.Message | None = None
        self._done = False

    def question(self) -> str:
        text = (
            f"**{self.inst.name}** already exists: `{self.inst.provider}` session "
            f"`{self.inst.session_id}` in `{self.inst.workdir}`. Resume that conversation in "
            "the new channel, or start a fresh session?"
        )
        if _instance_turn_running(self.inst):
            text += ("\n⏳ A turn for it is still running in its old channel. Resume becomes "
                     "possible once it finishes; Start fresh works now.")
        return text

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Only whoever ran `/new` can choose.", ephemeral=True)
            return False
        return True

    async def _choose(self, interaction: discord.Interaction, fresh: bool) -> None:
        if self._done:
            await interaction.response.send_message("Already chosen.", ephemeral=True)
            return
        if store.by_name(self.inst.name) is not self.inst:
            self._done = True
            await interaction.response.edit_message(
                content=f"**{self.inst.name}** was deleted in the meantime; run `/new` again.", view=None
            )
            return
        if not fresh and _instance_turn_running(self.inst):
            # Two turns on one session is exactly what makes the CLI refuse it.
            await interaction.response.send_message(
                "⏳ Its old channel still has a turn running on that session. Wait for it to "
                "finish and press Resume again, or Start fresh now.",
                ephemeral=True,
            )
            return
        self._done = True
        self.stop()
        await interaction.response.edit_message(
            content="✨ Starting fresh…" if fresh else "▶️ Resuming…", view=None
        )
        prompt = self.options.pop("prompt", None)
        channel, msg = await _reopen_instance(interaction.guild, self.inst, fresh=fresh, **self.options)
        await interaction.edit_original_response(content=msg)
        if prompt:
            await on_message_simple(channel, prompt)

    @discord.ui.button(label="Resume", emoji="▶️", style=discord.ButtonStyle.primary)
    async def resume(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._choose(interaction, fresh=False)

    @discord.ui.button(label="Start fresh", emoji="✨", style=discord.ButtonStyle.secondary)
    async def start_fresh(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._choose(interaction, fresh=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self._done = True
        self.stop()
        await interaction.response.edit_message(content="Cancelled; nothing changed.", view=None)

    async def on_timeout(self) -> None:
        if self.message is not None and not self._done:
            try:
                await self.message.edit(content="Timed out; nothing changed. Run `/new` again.", view=None)
            except discord.HTTPException:
                pass


# Agent CLIs refuse a session another process still has open (kimi says "in use
# by another Kimi Code CLI instance").
_SESSION_IN_USE_RE = re.compile(r"in use by another|session (is )?(already )?in use", re.IGNORECASE)


class StopTurnView(discord.ui.View):
    """⏹️ Stop on the "working…" message: the same graceful stop as `stop`."""

    def __init__(self, inst: Instance, turn: dict):
        super().__init__(timeout=None)  # removed by the turn's final edit
        self.inst = inst
        self.turn = turn

    @discord.ui.button(label="Stop", emoji="⏹️", style=discord.ButtonStyle.danger)
    async def stop_turn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        # Only this message's turn: an old button mustn't stop a newer turn.
        if _active_turns.get(self.inst.channel_id) is not self.turn:
            await interaction.response.send_message("That turn has already finished.", ephemeral=True)
            return
        if _interrupt(self.inst, interaction.user.display_name, drop_queue=True) is None:
            await interaction.response.send_message("It's already stopping.", ephemeral=True)
            return
        button.disabled = True
        button.label = "Stopping…"
        await interaction.response.edit_message(view=self)


class SessionInUseView(discord.ui.View):
    """Recovery buttons on a turn that failed because its session is locked elsewhere."""

    def __init__(self, inst: Instance, prompt: str):
        super().__init__(timeout=3600)
        self.inst = inst
        self.prompt = prompt
        self._done = False

    async def _retry(self, interaction: discord.Interaction, fresh: bool) -> None:
        if self._done:
            await interaction.response.send_message("Already handled.", ephemeral=True)
            return
        self._done = True
        self.stop()
        if fresh and store.get(self.inst.channel_id) is self.inst:
            self.inst.session_id = None
            store.put(self.inst)
            log.info("[%s] session in use elsewhere; starting fresh", self.inst.name)
        await interaction.response.edit_message(
            content=interaction.message.content + ("\n✨ Started a fresh session." if fresh else "\n🔁 Retrying."),
            view=None,
        )
        asyncio.get_running_loop().create_task(on_message_simple(interaction.channel, self.prompt))

    @discord.ui.button(label="Start fresh & retry", emoji="✨", style=discord.ButtonStyle.primary)
    async def fresh(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._retry(interaction, fresh=True)

    @discord.ui.button(label="Retry", emoji="🔁", style=discord.ButtonStyle.secondary)
    async def retry(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._retry(interaction, fresh=False)


async def _run_turn_recovering(inst: Instance, prompt: str, status_msg: discord.Message) -> None:
    """Run a turn; if the agent's CLI crashed before replying, restart and resend.

    First the same session again (a transient failure), then, if it crashes once
    more, a fresh session: the message isn't lost either way.
    """
    if await _run_turn(inst, prompt, status_msg) != "crashed":
        return
    channel = status_msg.channel
    log.warning("[%s] agent crashed before replying; retrying the same session", inst.name)
    retry = await channel.send("🔄 Restarting the session and resending your message…")
    if await _run_turn(inst, prompt, retry) != "crashed":
        return
    if not inst.session_id or store.get(inst.channel_id) is not inst:
        return
    old = inst.session_id
    inst.session_id = None
    store.put(inst)
    log.warning("[%s] session %s crashed twice; starting a fresh session", inst.name, old)
    await channel.send(
        f"⚠️ Session `{old}` crashed again, so your message goes to a **fresh session**. "
        "The old one isn't deleted: `/session` can switch back to it."
    )
    await _run_turn(inst, prompt, await channel.send("🤔 …"))


def _busy_notice(inst: Instance, *, steered: bool) -> str | None:
    """A one-per-turn note when a message has to wait for (or went into) a busy turn."""
    turn = _active_turns.get(inst.channel_id)
    if turn is None or turn.get("notified"):
        return None
    silent = time.time() - turn["last_event_at"]
    if steered and silent < STUCK_AFTER:
        return None  # ⏩ says enough while the agent is visibly working
    turn["notified"] = True
    elapsed = _fmt_duration(time.time() - turn["started_at"])
    how = "press ⏹️ Stop on its message, or send `! <your message>` to stop it and run yours"
    if steered:
        return (f"⏳ **{inst.name}** hasn't produced output for {_fmt_duration(silent)} "
                f"({elapsed} into this turn), so it may be stuck. Your message was passed to "
                f"it (⏩). To restart with your message, {how}.")
    stuck = " It may be stuck." if silent >= STUCK_AFTER else ""
    return (f"⏳ **{inst.name}** is still on the previous message ({elapsed} so far, last "
            f"output {_fmt_duration(silent)} ago).{stuck} Yours will run when it finishes; "
            f"to go now, {how}.")


async def on_message_simple(channel: discord.TextChannel, prompt: str) -> None:
    global _last_channel_id
    inst = store.get(channel.id)
    if inst is None:
        return
    _last_channel_id = channel.id
    if wa:
        _wa_task(wa.send_text(inst, f"💬 (Discord prompt):\n{prompt}"))
    async with _channel_lock(channel.id):
        async with _global_semaphore:
            async with channel.typing():
                status_msg = await channel.send("🤔 …")
                await _run_turn_recovering(inst, prompt, status_msg)


async def fire_trigger(trig: triggers.Trigger, event: dict) -> dict:
    """Run an external trigger's event as a turn (see harness/triggers.py).

    Same lifecycle as /new and /resume: an existing live instance gets the
    event as its next turn; a missing instance gets a new channel and session;
    a deleted channel is recreated; a /stop'ped instance is skipped unless the
    trigger says if_stopped=resume.
    """
    guild = client.get_guild(config.GUILD_ID)
    if guild is None:
        return {"status": "error", "reason": "guild not available"}
    name = triggers.instance_name(trig, event)
    inst = store.by_name(name) or next(
        (i for i in store.all() if i.name.lower() == name.lower()), None
    )
    if inst is not None:
        name = inst.name
    created = recreated = was_stopped = False
    if inst is not None and inst.stopped:
        if trig.if_stopped != "resume":
            return {"status": "skipped", "reason": f"instance '{name}' is stopped", "instance": name}
        inst.stopped = False
        was_stopped = True
    channel = guild.get_channel(inst.channel_id) if inst else None
    if inst is None:
        channel = await guild.create_text_channel(
            name=_slug(name), category=discord.Object(id=config.CATEGORY_ID),
            reason=f"trigger {trig.name}",
        )
        inst = Instance(
            channel_id=channel.id, name=name,
            provider=trig.provider or config.DEFAULT_PROVIDER,
            workdir=trig.workdir or config.DEFAULT_WORKDIR,
            model=trig.model, origin=f"trigger:{trig.name}",
        )
        inst.ensure_web_token()
        created = True
    elif not isinstance(channel, discord.TextChannel):
        channel = await guild.create_text_channel(
            name=_slug(name), category=discord.Object(id=config.CATEGORY_ID),
            reason=f"trigger {trig.name}",
        )
        inst.channel_id = channel.id
        recreated = True
    elif was_stopped:
        try:  # resumed after /stop: unlock like /resume does
            await channel.set_permissions(guild.default_role, send_messages=None)
        except discord.Forbidden:
            log.warning("no permission to unlock channel for %s", inst.name)
    store.put(inst)
    summary = ", ".join(f"{k}={triggers.clean_value(v)[:80]}" for k, v in list(event.items())[:6])
    await channel.send(
        f"⚡ trigger `{trig.name}` fired"
        + (" — new instance" if created else " — channel recreated" if recreated else "")
        + (f"\n> {summary}" if summary else "")
    )
    prompt = triggers.build_prompt(trig, event)
    asyncio.get_running_loop().create_task(_run_trigger_turn(channel, prompt, trig.name))
    return {"status": "started", "instance": name, "channel_id": str(channel.id),
            "created": created, "recreated": recreated}


async def _run_trigger_turn(channel: discord.TextChannel, prompt: str, trig_name: str) -> None:
    try:
        await on_message_simple(channel, prompt)
    except Exception:
        log.exception("trigger %s turn failed", trig_name)


@tree.command(name="triggers", description="List external triggers (manage them with python -m harness.triggers)")
async def cmd_triggers(interaction: discord.Interaction) -> None:
    items = triggers.TriggerStore().all()
    if not items:
        await interaction.response.send_message(
            "No triggers. Add one on the host: `.venv/bin/python -m harness.triggers add <name> ...`"
        )
        return
    lines = [
        f"• `{t.name}` → instance `{t.instance}` ({t.provider or config.DEFAULT_PROVIDER}"
        f"{'/' + t.model if t.model else ''}), if stopped: {t.if_stopped}, "
        f"{len(t.fired_at)}/{t.max_per_hour} in the last hour"
        for t in items
    ]
    url = f"{config.PUBLIC_BASE_URL}/agents/_triggers/<name>" if config.PUBLIC_BASE_URL else "(PUBLIC_BASE_URL not set)"
    await interaction.response.send_message("\n".join(lines) + f"\nEndpoint: `POST {url}`")


@tree.command(name="files", description="List this channel's shared files (charts, logs, reports) with links")
@discord.app_commands.describe(folder="Only this subfolder (e.g. charts)")
async def cmd_files(interaction: discord.Interaction, folder: str | None = None) -> None:
    inst = store.get(interaction.channel_id) if interaction.channel_id else None
    if inst is None:
        await interaction.response.send_message("❌ run this inside an agent channel")
        return
    slug = _slug(inst.name)
    token = inst.ensure_web_token()
    store.put(inst)
    base = files.files_url(slug, token)
    entries = files.listing(slug, folder or "")
    head = f"📁 `{files.files_dir(slug)}`" + (f" — index: {base}" if base else "")
    if not entries:
        await interaction.response.send_message(head + "\n(no files yet)")
        return
    msg, shown = head, 0
    for e in entries:  # newest first, until the message is full
        line = (f"\n• [{e.path}](<{base}{e.path}>) · {files.human_size(e.size)}" if base
                else f"\n• `{e.path}` · {files.human_size(e.size)}")
        if len(msg) + len(line) > DISCORD_MSG_LIMIT - 60:
            break
        msg, shown = msg + line, shown + 1
    if shown < len(entries):
        msg += f"\n…and {len(entries) - shown} older (see the index)"
    await interaction.response.send_message(msg)


def _channel_alive(inst: Instance) -> bool:
    return isinstance(client.get_channel(inst.channel_id), discord.TextChannel)


def _name_choices(names: list[str], current: str) -> list[discord.app_commands.Choice[str]]:
    return [
        discord.app_commands.Choice(name=n, value=n)
        for n in names
        if current.lower() in n.lower()
    ][:25]


async def _live_name_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[discord.app_commands.Choice[str]]:
    """Instance names that are active with an existing channel (for /stop, /delete)."""
    return _name_choices(
        [i.name for i in store.all() if not i.stopped and _channel_alive(i)], current
    )


async def _resumable_name_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[discord.app_commands.Choice[str]]:
    """Tracked instances needing a resume: stopped, or their channel was deleted."""
    return _name_choices(
        [i.name for i in store.all() if i.stopped or not _channel_alive(i)], current
    )


@tree.command(name="stop", description="Close an agent channel (session is kept for resume)")
@discord.app_commands.describe(name="Instance name to stop")
@discord.app_commands.autocomplete(name=_live_name_autocomplete)
async def cmd_stop(interaction: discord.Interaction, name: str) -> None:
    await interaction.response.defer()
    inst = store.by_name(name)
    if not inst:
        await interaction.followup.send(f"❌ no instance named '{name}'")
        return
    channel = interaction.guild.get_channel(inst.channel_id) if interaction.guild else None
    inst.stopped = True
    store.put(inst)
    log.info("[%s] instance stopped by %s", inst.name, interaction.user)
    if wa:
        _wa_task(wa.send_text(inst, f"⏸️ {name} stopped — session kept; `/resume name:{name}` in Discord."))
    if isinstance(channel, discord.TextChannel):
        try:
            await channel.set_permissions(interaction.guild.default_role, send_messages=False)
        except discord.Forbidden:
            await interaction.followup.send(
                f"⚠️ stopped '{name}' (it will no longer respond or appear active in "
                f"`/list`), but couldn't lock the channel — the bot lacks the "
                f"**Manage Roles** permission. Session retained; `/resume name:{name}` reopens."
            )
            return
    await interaction.followup.send(
        f"✅ stopped '{name}' — channel locked, session retained. "
        f"Use `/resume name:{name}` to bring it back."
    )


@tree.command(name="resume", description="Bring back a stopped instance, recreating its channel if deleted")
@discord.app_commands.describe(name="Tracked instance name (stopped or channel deleted)")
@discord.app_commands.autocomplete(name=_resumable_name_autocomplete)
async def cmd_resume(interaction: discord.Interaction, name: str) -> None:
    await interaction.response.defer()
    inst = store.by_name(name)
    if not inst:
        await interaction.followup.send(f"❌ no tracked instance named '{name}'")
        return
    guild = interaction.guild
    assert guild is not None
    channel = guild.get_channel(inst.channel_id)
    if isinstance(channel, discord.TextChannel) and not inst.stopped:
        await interaction.followup.send(f"ℹ️ '{name}' is already live in {channel.mention}")
        return
    recreated = False
    if isinstance(channel, discord.TextChannel):
        # Channel exists but the instance was stopped: unlock it.
        try:
            await channel.set_permissions(guild.default_role, send_messages=None)
        except discord.Forbidden:
            log.warning("no permission to unlock channel for %s", inst.name)
    else:
        # Channel was deleted in Discord; recreate it for the tracked instance.
        channel = await guild.create_text_channel(
            name=_slug(name), category=discord.Object(id=config.CATEGORY_ID)
        )
        inst.channel_id = channel.id
        recreated = True
    inst.stopped = False
    store.put(inst)
    if wa:
        if inst.wa_group:
            _wa_task(wa.send_text(inst, f"▶️ {name} resumed."))
        else:
            _wa_task(wa.ensure_group(inst))
    log.info(
        "[%s] instance resumed by %s (channel %s)",
        inst.name, interaction.user, "recreated" if recreated else "unlocked",
    )
    await interaction.followup.send(
        f"✅ resumed '{name}' in {channel.mention} — session `{inst.session_id}` "
        "resumes on the next message."
    )


@tree.command(name="delete", description="Stop and permanently delete an instance (channel, store entry)")
@discord.app_commands.describe(name="Instance name to delete (auto-stops it first if active)")
@discord.app_commands.autocomplete(name=_live_name_autocomplete)
async def cmd_delete(interaction: discord.Interaction, name: str) -> None:
    await interaction.response.defer()
    inst = store.by_name(name)
    if not inst:
        await interaction.followup.send(f"❌ no instance named '{name}'")
        return
    was_active = not inst.stopped
    inst.stopped = True
    log.info("[%s] instance deleted by %s (auto-stopped: %s)", inst.name, interaction.user, was_active)
    channel = interaction.guild.get_channel(inst.channel_id) if interaction.guild else None
    if isinstance(channel, discord.TextChannel):
        try:
            await channel.delete(reason=f"/delete {name}")
        except discord.Forbidden:
            log.warning("no permission to delete channel for %s", inst.name)
    store.remove(inst.channel_id)
    if wa and inst.wa_group:
        await wa.send_text(inst, f"🗑️ {name} deleted — this group is no longer connected.")
        await wa.leave_group(inst)
    note = " (auto-stopped first)" if was_active else ""
    await interaction.followup.send(
        f"🗑️ deleted '{name}'{note} — channel removed and instance forgotten. "
        "(The provider's own session files are not touched.)"
    )


@tree.command(name="interrupt", description="Stop the agent's current turn (session kept), optionally with new instructions")
@discord.app_commands.describe(
    message="What to do instead: sent to the agent as soon as the turn has stopped",
    keep_queue="Keep messages already waiting behind the turn (default: drop them)",
)
async def cmd_interrupt(
    interaction: discord.Interaction, message: str | None = None, keep_queue: bool = False
) -> None:
    inst = store.get(interaction.channel_id) if interaction.channel_id else None
    if inst is None:
        await interaction.response.send_message("❌ run this inside an agent channel", ephemeral=True)
        return
    result = _interrupt(inst, interaction.user.display_name, drop_queue=not keep_queue)
    if result is None and not message:
        await interaction.response.send_message("Nothing is running in this channel.", ephemeral=True)
        return
    lines = []
    if result is not None:
        _, dropped = result
        lines.append(f"⏹️ **{interaction.user.display_name}** stopped the current turn; the session is kept.")
        if dropped:
            lines.append(f"🗑️ Dropped {dropped} queued message{'s' if dropped != 1 else ''}.")
    if message:
        lines.append(f"➡️ Next: {message}")
    await interaction.response.send_message(discord_markdown.split_message("\n".join(lines), DISCORD_MSG_LIMIT)[0])
    if message:
        asyncio.get_running_loop().create_task(on_message_simple(interaction.channel, message))


@tree.command(name="model", description="Show or change the provider/model for this channel's agent")
@discord.app_commands.describe(
    provider="Switch agent CLI (starts a fresh session; omit to keep the current one)",
    model="provider:model from the list, or a bare alias/ID (omit to show the current model)",
)
@discord.app_commands.choices(
    provider=[discord.app_commands.Choice(name=p, value=p) for p in provider_names()]
)
@discord.app_commands.autocomplete(model=_model_autocomplete)
async def cmd_model(
    interaction: discord.Interaction, provider: str | None = None, model: str | None = None
) -> None:
    inst = store.get(interaction.channel_id) if interaction.channel_id else None
    if inst is None:
        await interaction.response.send_message("❌ run this inside an agent channel")
        return
    try:
        provider, model = _split_model(provider, model)
    except ValueError as e:
        await interaction.response.send_message(f"❌ {e}")
        return
    log.info(
        "[%s] /model by %s: current=%s/%s requested=%s/%s",
        inst.name, interaction.user, inst.provider, _model_label(inst), provider, model,
    )
    switching = provider is not None and provider != inst.provider
    if model is None and not switching:
        await interaction.response.send_message(
            f"**{inst.name}**: provider `{inst.provider}`, model `{_model_label(inst)}`"
        )
        return
    old = f"{inst.provider}/{_model_label(inst)}"
    if switching:
        # Session ids are provider-specific, so a provider switch starts over.
        inst.provider = provider
        inst.session_id = None
    inst.model = model
    store.put(inst)
    log.info("[%s] model changed: %s -> %s/%s", inst.name, old, inst.provider, _model_label(inst))
    await interaction.response.send_message(
        f"✅ **{inst.name}** now uses `{inst.provider}` / `{_model_label(inst)}` from the next "
        + ("message (**new session** — previous conversation not carried over)." if switching
           else "message (same session).")
    )


def _session_label(info: SessionInfo) -> str:
    title = info.title or "(untitled)"
    return f"{title[:50]} · {_display_path(info.workdir)}"[:100]


def _display_path(path: str) -> str:
    home = os.path.expanduser("~")
    return "~" + path[len(home):].replace("\\", "/") if path.startswith(home) else path


async def _session_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[discord.app_commands.Choice[str]]:
    """This channel's provider sessions, newest first, filtered by id/title/folder."""
    inst = store.get(interaction.channel_id) if interaction.channel_id else None
    if inst is None:
        return []
    needle = current.lower()
    choices = []
    for info in await asyncio.to_thread(get_provider(inst.provider).sessions):
        if needle in f"{info.id} {info.title} {info.workdir}".lower():
            mark = "▶ " if info.id == inst.session_id else ""
            choices.append(discord.app_commands.Choice(name=mark + _session_label(info), value=info.id))
        if len(choices) == 25:
            break
    return choices


@tree.command(
    name="session",
    description="Show this channel's agent session, or continue an existing one (e.g. from a terminal)",
)
@discord.app_commands.describe(
    session="Session to continue here (its folder becomes the workdir); omit to show the current one",
)
@discord.app_commands.autocomplete(session=_session_autocomplete)
async def cmd_session(interaction: discord.Interaction, session: str | None = None) -> None:
    inst = store.get(interaction.channel_id) if interaction.channel_id else None
    if inst is None:
        await interaction.response.send_message("❌ run this inside an agent channel")
        return
    if session is None:
        await interaction.response.send_message(
            f"**{inst.name}**: `{inst.provider}` session `{inst.session_id or 'new'}` in `{inst.workdir}`"
        )
        return
    session = session.strip()
    if inst.channel_id in _active_turns:
        await interaction.response.send_message(
            "❌ a turn is running in this channel — wait for it to finish first"
        )
        return
    other = next(
        (i for i in store.all() if i.session_id == session and i.channel_id != inst.channel_id), None
    )
    if other is not None:
        await interaction.response.send_message(
            f"❌ `{session}` is already used by instance **{other.name}** — "
            "two channels driving one session would collide"
        )
        return
    provider = get_provider(inst.provider)
    info = await asyncio.to_thread(provider.find_session, session)
    if info is None:
        await interaction.response.send_message(
            f"❌ no `{inst.provider}` session `{session}` found — pick one from the list "
            "(`/model provider:` first if it belongs to another CLI)"
        )
        return
    old = (inst.session_id, inst.workdir)
    inst.session_id = info.id
    inst.workdir = info.workdir
    inst.__post_init__()  # normalize workdir the same way loading does
    store.put(inst)
    log.info(
        "[%s] /session by %s: %s in %s -> %s in %s",
        inst.name, interaction.user, old[0], old[1], inst.session_id, inst.workdir,
    )
    await interaction.response.send_message(
        f"✅ **{inst.name}** continues `{inst.provider}` session `{info.id}`"
        + (f" ({info.title})" if info.title else "")
        + f" in `{inst.workdir}` from the next message. "
        "Close that session anywhere else it's open (e.g. a terminal) first."
    )


async def _provider_usage(name: str) -> str:
    try:
        text = await get_provider(name).usage()
    except Exception as e:
        log.warning("usage for %s failed: %s", name, e)
        text = f"❌ {e}"
    return f"**{name}**\n```\n{text[:1700]}\n```"


# Sign-ins in progress, one per CLI (a new /login for the same CLI replaces it).
_logins: dict[str, logins.LoginSession] = {}


def _login_result(name: str, st: logins.Status, ok: bool, session: logins.LoginSession) -> str:
    if st.logged_in and st.subscription:
        return f"✅ **{logins.LABELS[name]}** is signed in: {st.detail}."
    if st.logged_in:
        return f"⚠️ **{logins.LABELS[name]}** is signed in, but with {st.detail}, not a subscription."
    why = session.result_text() or ("timed out" if not ok else "unknown")
    return f"❌ **{logins.LABELS[name]}** sign-in didn't complete: {why}"


async def _finish_login(interaction: discord.Interaction, session: logins.LoginSession) -> None:
    """For device-code logins: report once you've approved in the browser."""
    ok = await session.wait()
    if _logins.get(session.name) is session:
        _logins.pop(session.name)
    msg = _login_result(session.name, await logins.status(session.name), ok, session)
    try:
        await interaction.followup.send(msg, ephemeral=True)
    except discord.HTTPException:  # interaction token expired (15 min)
        channel = client.get_channel(interaction.channel_id)
        if channel is not None:
            await channel.send(msg)


@tree.command(name="login", description="Agent sign-in: show status, or log in with your subscription")
@discord.app_commands.describe(
    provider="CLI to sign in (omit to see every CLI's status)",
    code="Claude only: the code the sign-in page shows after you approve",
    again="Sign in again even if already signed in with a subscription",
)
@discord.app_commands.choices(
    provider=[discord.app_commands.Choice(name=logins.LABELS[p], value=p) for p in logins.PROVIDERS]
)
@discord.app_commands.default_permissions(administrator=True)
async def cmd_login(
    interaction: discord.Interaction,
    provider: str | None = None,
    code: str | None = None,
    again: bool = False,
) -> None:
    await interaction.response.defer(ephemeral=True)
    if provider is None:
        statuses = await asyncio.gather(*(logins.status(p) for p in logins.PROVIDERS))
        lines = [
            f"{'✅' if st.subscription else '⚠️' if st.logged_in else '❌' if st.installed else '➖'} "
            f"**{logins.LABELS[p]}**: {st.detail}"
            for p, st in zip(logins.PROVIDERS, statuses)
        ]
        if config.IGNORED_API_KEYS:
            lines.append(f"🔑 ignoring API keys set on this machine: {', '.join(config.IGNORED_API_KEYS)}")
        elif config.ALLOW_AGENT_API_KEYS:
            lines.append("🔑 `ALLOW_AGENT_API_KEYS` is on: API keys in the environment are used")
        lines.append("Sign in with `/login provider:<name>`.")
        await interaction.followup.send("\n".join(lines), ephemeral=True)
        return

    label = logins.LABELS[provider]
    if code:
        session = _logins.get(provider)
        if session is None or not session.needs_code or session.done:
            await interaction.followup.send(
                f"No {label} sign-in is waiting for a code. Start one with `/login provider:{provider}`.",
                ephemeral=True,
            )
            return
        log.info("/login %s: code submitted by %s", provider, interaction.user)
        await session.submit_code(code)
        ok = await session.wait(timeout=90)
        _logins.pop(provider, None)
        st = await logins.status(provider)
        await interaction.followup.send(_login_result(provider, st, ok, session), ephemeral=True)
        return

    st = await logins.status(provider)
    if not st.installed:
        await interaction.followup.send(
            f"{label} isn't installed on this machine. Rerun the setup script to install it.",
            ephemeral=True,
        )
        return
    if st.subscription and not again:
        await interaction.followup.send(
            f"✅ {label} is already signed in: {st.detail}. Use `again:True` to sign in again.",
            ephemeral=True,
        )
        return
    if (old := _logins.pop(provider, None)) is not None:
        old.cancel()
    session = logins.LoginSession(provider)
    log.info("/login %s started by %s", provider, interaction.user)
    try:
        await session.start()
    except (logins.LoginError, OSError) as e:
        await interaction.followup.send(f"❌ {e}", ephemeral=True)
        return
    _logins[provider] = session
    steps = [f"1. Open <{session.url}> and sign in with the account that has your {label} subscription."]
    if session.confirm_code:
        steps.append(f"2. Check the page shows the code **{session.confirm_code}**, then approve.")
    if session.needs_code:
        steps.append(
            f"2. After you approve, the page shows a code. Send it with "
            f"`/login provider:{provider} code:<the code>`."
        )
    else:
        steps.append("3. I'll confirm here once it completes.")
        asyncio.get_running_loop().create_task(_finish_login(interaction, session))
    await interaction.followup.send("\n".join(steps), ephemeral=True)


@tree.command(name="usage", description="Show plan usage / remaining quota as the agent CLI reports it")
@discord.app_commands.describe(
    provider="Which CLI to ask (default: this channel's provider, or all outside agent channels)"
)
@discord.app_commands.choices(
    provider=[discord.app_commands.Choice(name=p, value=p) for p in [*provider_names(), "all"]]
)
async def cmd_usage(interaction: discord.Interaction, provider: str | None = None) -> None:
    await interaction.response.defer()
    inst = store.get(interaction.channel_id) if interaction.channel_id else None
    if provider is None:
        provider = inst.provider if inst else "all"
    names = provider_names() if provider == "all" else [provider]
    log.info("/usage by %s: %s", interaction.user, ", ".join(names))
    for block in await asyncio.gather(*(_provider_usage(n) for n in names)):
        await interaction.followup.send(block)


@tree.command(name="whatsapp", description="WhatsApp mirror status, or link the bot account")
@discord.app_commands.describe(
    phone="Bot account's number (with country code); default WHATSAPP_BOT_NUMBER, else a QR"
)
@discord.app_commands.default_permissions(administrator=True)
async def cmd_whatsapp(interaction: discord.Interaction, phone: str | None = None) -> None:
    if not config.WHATSAPP_ENABLED:
        await interaction.response.send_message(
            "WhatsApp mirror is off — set `WHATSAPP_OWNER` in `.env` and restart."
        )
        return
    if wa is None:
        await interaction.response.send_message("❌ WhatsApp bridge failed to start (see logs).")
        return
    if wa.linked:
        groups = [i for i in store.all() if i.wa_group]
        await interaction.response.send_message(
            f"✅ WhatsApp linked — {len(groups)} group(s) mirrored to owner `{config.WHATSAPP_OWNER}`."
        )
        return
    phone = phone or config.WHATSAPP_BOT_NUMBER
    if phone:
        await interaction.response.defer()
        try:
            code = await wa.pair_code(phone)
        except Exception as e:
            await interaction.followup.send(f"❌ pairing code failed: {e}")
            return
        await interaction.followup.send(
            f"On the **bot** phone: WhatsApp → Linked devices → Link a device → "
            f"*Link with phone number instead* → enter **`{code}`**"
        )
        return
    png = wa.qr_png()
    if png is None:
        await interaction.response.send_message("⏳ still connecting to WhatsApp — try again in a few seconds.")
        return
    await interaction.response.send_message(
        "Scan with the **bot** phone (WhatsApp → Linked devices → Link a device). "
        "QR codes rotate quickly — rerun `/whatsapp` if it expires, or pass `phone:` for a code.",
        file=discord.File(io.BytesIO(png), filename="whatsapp-qr.png"),
    )


@tree.command(name="logs", description="Post this machine's recent harness logs as a file (secrets removed)")
@discord.app_commands.describe(lines="How many recent lines from each log (default 3000)")
@discord.app_commands.default_permissions(administrator=True)
async def cmd_logs(interaction: discord.Interaction, lines: int = 3000) -> None:
    await interaction.response.defer()
    header = [f"bot {client.user} · {len(_active_turns)} turn(s) running"] + [
        f"instance {i.name}: {i.provider}/{_model_label(i)} session={i.session_id} "
        f"workdir={i.workdir}{' (stopped)' if i.stopped else ''}"
        for i in store.all()
    ]
    text = await asyncio.to_thread(logfile.export, max(100, min(lines, 20000)), header)
    data, name = text.encode(), f"logs-{_slug(socket.gethostname())}-{time.strftime('%Y%m%d-%H%M%S')}.txt"
    if len(data) > 7_000_000:  # under Discord's upload limit
        data, name = gzip.compress(data), name + ".gz"
    log.info("/logs by %s: %d bytes", interaction.user, len(data))
    await interaction.followup.send(
        f"📄 Logs from **{socket.gethostname()}** ({client.user.name}), secrets removed. "
        "Hand this to whoever is debugging, or tell the agent on another machine where it is.",
        file=discord.File(io.BytesIO(data), filename=name),
    )


@tree.command(name="uptime", description="Internet and system uptime: outages, availability, history")
@discord.app_commands.describe(recent="How many recent outages to list (default 5)")
async def cmd_uptime(interaction: discord.Interaction, recent: int = 5) -> None:
    text = uptime.summary(recent=max(0, min(recent, 25)))
    await interaction.response.send_message(text[:DISCORD_MSG_LIMIT])


@tree.command(name="list", description="List all agent instances")
async def cmd_list(interaction: discord.Interaction) -> None:
    active = [i for i in store.all() if not i.stopped and _channel_alive(i)]
    resumable = [i for i in store.all() if i.stopped or not _channel_alive(i)]
    lines = [
        f"• **{i.name}** — `{i.provider}` / `{_model_label(i)}` @ `{i.workdir}`"
        + (f" (session: `{i.session_id}`)" if i.session_id else " (no session yet)")
        for i in active
    ]
    if resumable:
        lines += ["", "_inactive — `/resume name:<name>` brings it back, `/delete name:<name>` removes for good:_"]
        lines += [
            f"• **{i.name}**" + ("" if i.stopped else " (channel deleted)")
            for i in resumable
        ]
    await interaction.response.send_message("\n".join(lines) or "no instances yet")


async def _run_cmd(*args: str, timeout: float = 300) -> tuple[int, str]:
    """Run a command in the repo dir; return (returncode, combined output)."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=config.BASE_DIR,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 1, f"`{' '.join(args)}` timed out after {timeout:.0f}s"
    return proc.returncode or 0, out.decode("utf-8", "replace").strip()


def _restart_harness() -> None:
    """Replace the running harness with a fresh one (after a code update).

    Under systemd the unit is restarted (its watchdog unit follows through
    BindsTo/Wants); otherwise the process re-execs itself in place.
    """
    unit = service.current_unit()
    log.warning("restarting harness (%s)", unit or "exec")
    if unit:
        # The uptime monitor isn't bound to the bot; restart it so it runs new code too.
        subprocess.run(
            ["systemctl", "--user", "--no-block", "try-restart", service.UPTIME_UNIT], check=False
        )
        # --no-block queues the job and returns before systemd kills this process.
        subprocess.Popen(["systemctl", "--user", "--no-block", "restart", unit])
    else:
        os.chdir(config.BASE_DIR)
        os.execv(sys.executable, [sys.executable, "-m", "harness.bot"])


async def _announce_update() -> None:
    """After a /update restart, confirm in the channel that asked for it."""
    if not UPDATE_MARKER.exists():
        return
    try:
        info = json.loads(UPDATE_MARKER.read_text())
        UPDATE_MARKER.unlink()
        _, head = await _run_cmd("git", "log", "-1", "--format=%h %s")
        channel = client.get_channel(info["channel_id"])
        if channel is not None:
            await channel.send(f"✅ harness back online on `{head}` (was `{info['old']}`)")
    except Exception:
        log.exception("could not announce update")


@tree.command(name="update", description="Pull the latest harness code from GitHub and restart")
@discord.app_commands.describe(
    force="Restart even if already up to date or while agent turns are running"
)
@discord.app_commands.default_permissions(administrator=True)
async def cmd_update(interaction: discord.Interaction, force: bool = False) -> None:
    if _active_turns and not force:
        names = ", ".join(t["instance"] for t in _active_turns.values())
        await interaction.response.send_message(
            f"⏳ {len(_active_turns)} turn(s) running ({names}); a restart would kill them. "
            "Retry when they finish, or use `force:True`."
        )
        return
    await interaction.response.defer()
    log.info("/update by %s (force=%s)", interaction.user, force)

    _, old = await _run_cmd("git", "rev-parse", "--short", "HEAD")
    rc, pull_out = await _run_cmd("git", "pull", "--ff-only")
    if rc != 0:
        await interaction.followup.send(f"❌ `git pull` failed, not restarting:\n```\n{pull_out[-1500:]}\n```")
        return
    _, new = await _run_cmd("git", "rev-parse", "--short", "HEAD")
    if old == new and not force:
        await interaction.followup.send(f"✅ already up to date at `{new}` — not restarting.")
        return

    lines = [f"⬇️ `{old}` → `{new}`"]
    if old != new:
        _, log_out = await _run_cmd("git", "log", "--format=• %h %s", f"{old}..{new}")
        lines.append(log_out[:1200])
        _, changed = await _run_cmd("git", "diff", "--name-only", old, new)
        if "requirements.txt" in changed.split():
            rc, pip_out = await _run_cmd(*service.pip_install_cmd())
            if rc != 0:
                await interaction.followup.send(
                    "\n".join(lines)
                    + f"\n❌ dependency install failed, not restarting:\n```\n{pip_out[-1200:]}\n```"
                )
                return
            lines.append("📦 dependencies updated")
    # Unit-file sync runs from the new code, so template changes land with the pull.
    rc, units_out = await _run_cmd(sys.executable, "-m", "harness.service")
    if rc != 0:
        await interaction.followup.send(
            "\n".join(lines)
            + f"\n❌ service file sync failed, not restarting:\n```\n{units_out[-1200:]}\n```"
        )
        return
    if units_out.startswith("updated"):
        lines.append(f"🛠️ systemd units {units_out}")
    lines.append("🔄 restarting…")
    await interaction.followup.send("\n".join(lines))

    UPDATE_MARKER.write_text(json.dumps({"channel_id": interaction.channel_id, "old": old}))
    await asyncio.sleep(1)  # let the followup flush before the process goes away
    _restart_harness()


@tree.error
async def on_command_error(interaction: discord.Interaction, error: Exception) -> None:
    """Never leave a deferred interaction hanging at 'thinking...'."""
    log.exception("command failed: %s", getattr(interaction.command, "name", "?"))
    msg = f"❌ {str(error)[:500]}"
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg)
        else:
            await interaction.response.send_message(msg)
    except discord.HTTPException:
        pass


def main() -> None:
    if not config.BOT_TOKEN:
        raise SystemExit("DISCORD_BOT_TOKEN is not set — copy .env.example to .env and fill it in")
    if not config.GUILD_ID or not config.CATEGORY_ID:
        raise SystemExit("DISCORD_GUILD_ID and DISCORD_CATEGORY_ID must be set in .env")
    # A plain `git pull` + restart (no /update) should still bring unit changes in.
    try:
        changed = service.sync()
        if changed:
            log.warning("systemd units updated: %s (env changes apply on next restart)", ", ".join(changed))
    except Exception:
        log.exception("systemd unit sync failed")
    client.run(config.BOT_TOKEN)


if __name__ == "__main__":
    main()

"""Discord bot: one channel per agent instance, one message per turn.

Messages in a managed channel are forwarded to the instance's provider as a
turn; the reply streams into a single bot message (edited in place), and the
channel topic shows the agent's last message. If the harness (or the whole
machine) restarts, the next message in the channel transparently resumes the
stored provider session.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import subprocess
import sys
import time

import discord

from . import config, service
from .instances import Instance, InstanceManager
from .providers.base import get_provider, provider_names
from .render import render_html_to_png

if config.PUBLIC_BASE_URL:
    from . import proxy

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("discord_agent")

DISCORD_MSG_LIMIT = 2000
TOPIC_LIMIT = 1024
EDIT_INTERVAL = 1.0  # seconds between in-place message edits

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

client = discord.Client(intents=intents)
tree = discord.app_commands.CommandTree(client)

store = InstanceManager()

# Serialize turns within a channel; cap total concurrent turns globally.
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
    if config.PUBLIC_BASE_URL:
        await proxy.start_proxy(store)
    asyncio.get_running_loop().create_task(_heartbeat_loop())


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
        try:
            await channel.send(
                f"⏳ **{inst.name}** still working — {elapsed} elapsed, "
                f"{turn['tools']} tool calls, last output {silent} ago "
                f"(model `{turn['model']}`)"
            )
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
    try:
        out = subprocess.run(
            ["ss", "-tlnH"], capture_output=True, text=True, timeout=5
        ).stdout
    except Exception:
        return set()
    ports = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4:
            tail = parts[3].rsplit(":", 1)[-1]
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
You are running inside a Discord agent harness on a Linux host.
Working directory: {workdir}
Output formats you can choose from, in order of richness:
1. Live web page — for interactive or visual output (demos, dashboards, forms,
   games, visualizations). Build the page, then serve it in the background on a
   port in the range {port_start}-{port_end}, detached so it outlives your turn:
       cd <dir> && setsid nohup python3 -m http.server <port> >/dev/null 2>&1 &
   (or any small server). The harness detects the server and posts a public URL
   in the Discord channel automatically — you do not need to construct URLs.
   Use this format whenever the result benefits from being interactive.
2. Static HTML in a fenced ```html code block — the harness renders it to an
   image. Good for one-shot rich documents (tables, reports, layouts).
3. Plain Markdown text — for everything else.
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
[end of harness capabilities]
"""


def _with_brief(inst: Instance, prompt: str) -> str:
    """Prepend the capability brief when this turn starts a new session."""
    if inst.session_id is not None:
        return prompt
    return _HARNESS_BRIEF.format(
        workdir=inst.workdir,
        port_start=config.AGENT_PORT_START,
        port_end=config.AGENT_PORT_END,
        annotations_path=config.DATA_DIR / "annotations" / f"{_slug(inst.name)}.json",
    ) + "\n" + prompt


async def _run_turn(inst: Instance, prompt: str, status_msg: discord.Message) -> None:
    """Run one provider turn for an instance, streaming into status_msg."""
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
    }
    _active_turns[inst.channel_id] = turn
    progress = asyncio.get_running_loop().create_task(
        _progress_updates(inst, status_msg.channel, turn)
    )

    def display(text: str) -> str:
        text = text.strip()
        return text if len(text) <= DISCORD_MSG_LIMIT else text[: DISCORD_MSG_LIMIT - 20] + "\n…(truncated)"

    try:
        try:
            agen = provider.run_turn(
                inst.workdir, _with_brief(inst, prompt), inst.session_id, inst.model
            )
            while True:
                try:
                    event = await agen.__anext__()
                except StopAsyncIteration:
                    break
                turn["last_event_at"] = time.time()
                if event.kind == "text":
                    buf += event.text
                elif event.kind == "status":
                    turn["tools"] += 1
                    log.info("[%s] %s", inst.name, event.text)
                elif event.kind == "error":
                    error = event.text
                elif event.kind == "done":
                    buf = event.text or buf
                    if (
                        event.session_id
                        and event.session_id != inst.session_id
                        and store.get(inst.channel_id) is inst  # not /delete'd mid-turn
                    ):
                        inst.session_id = event.session_id
                        store.put(inst)
                        log.info("[%s] session id captured: %s", inst.name, inst.session_id)

                now = loop.time()
                if buf and now - last_edit >= EDIT_INTERVAL:
                    last_edit = now
                    await status_msg.edit(content=display(buf) or "…")
        except Exception:
            log.exception("turn failed for %s", inst.name)
            error = "harness error while running the agent (see logs)"

        if error:
            log.warning(
                "[%s] turn failed after %.0fs: %.300s",
                inst.name, loop.time() - started, error,
            )
            await status_msg.edit(content=f"❌ {display(error)}")
            return

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
                final_text += "\n\n(html rendering failed — see raw source above)"

        # Final content: split across messages if over the limit.
        chunks = [
            final_text[i : i + DISCORD_MSG_LIMIT] for i in range(0, len(final_text), DISCORD_MSG_LIMIT)
        ]
        if image:
            await status_msg.edit(content=chunks[0], attachments=[image])
        else:
            await status_msg.edit(content=chunks[0])
        for chunk in chunks[1:]:
            await status_msg.channel.send(chunk)
        if source:
            await status_msg.channel.send(file=source)

        topic = final_text[:TOPIC_LIMIT]
        try:
            await status_msg.channel.edit(topic=topic)
        except discord.Forbidden:
            log.warning("missing permission to set channel topic for %s", inst.name)
    finally:
        _active_turns.pop(inst.channel_id, None)
        progress.cancel()


@client.event
async def on_ready() -> None:
    guild = discord.Object(id=config.GUILD_ID)
    tree.copy_global_to(guild=guild)
    await tree.sync(guild=guild)
    log.info("logged in as %s", client.user)
    asyncio.get_running_loop().create_task(_announce_update())
    for inst in store.all():
        asyncio.get_running_loop().create_task(_catch_up(inst))


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
    await _process_user_message(inst, message)


async def _process_user_message(inst: Instance, message: discord.Message) -> None:
    global _last_channel_id
    _last_channel_id = message.channel.id
    try:
        async with _global_semaphore:
            async with _channel_lock(message.channel.id):
                async with message.channel.typing():
                    status_msg = await message.channel.send("🤔 …")
                    await _run_turn(inst, message.content, status_msg)
        if store.get(message.channel.id) is inst:  # not /delete'd mid-turn
            inst.last_message_id = str(message.id)
            store.put(inst)
        await message.add_reaction("✅")
    except Exception:
        log.exception("message turn failed for %s", inst.name)
        await message.add_reaction("❌")


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
    workdir="Working directory for the agent (default: DEFAULT_WORKDIR)",
    prompt="Optional first prompt to start the agent with",
    provider="Agent CLI to use (default: DEFAULT_PROVIDER)",
    model="provider:model from the list, or a bare alias/ID (default: the provider's default)",
)
@discord.app_commands.choices(
    provider=[discord.app_commands.Choice(name=p, value=p) for p in provider_names()]
)
@discord.app_commands.autocomplete(model=_model_autocomplete)
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
        # Reopening a stopped/archived instance: new channel, same session.
        channel = await guild.create_text_channel(
            name=_slug(name), category=discord.Object(id=config.CATEGORY_ID)
        )
        existing.channel_id = channel.id
        existing.stopped = False
        if provider and provider != existing.provider:
            # Session ids are provider-specific, so a provider switch starts over.
            existing.provider = provider
            existing.session_id = None
            existing.model = model
        elif model:
            existing.model = model
        store.put(existing)
        await interaction.followup.send(
            f"✅ reopened {channel.mention} — "
            + (f"resuming `{existing.provider}` session `{existing.session_id}`"
               if existing.session_id else f"new `{existing.provider}` session")
            + f" on next message (model: `{_model_label(existing)}`)."
        )
        if prompt:
            await on_message_simple(channel, prompt)
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
    await interaction.followup.send(
        f"✅ created {channel.mention} — just chat in it. "
        f"Provider: `{inst.provider}`, model: `{_model_label(inst)}`, workdir: `{inst.workdir}`"
    )
    if prompt:
        await on_message_simple(channel, prompt)


async def on_message_simple(channel: discord.TextChannel, prompt: str) -> None:
    global _last_channel_id
    inst = store.get(channel.id)
    if inst is None:
        return
    _last_channel_id = channel.id
    async with _global_semaphore:
        async with _channel_lock(channel.id):
            async with channel.typing():
                status_msg = await channel.send("🤔 …")
                await _run_turn(inst, prompt, status_msg)


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
    note = " (auto-stopped first)" if was_active else ""
    await interaction.followup.send(
        f"🗑️ deleted '{name}'{note} — channel removed and instance forgotten. "
        "(The provider's own session files are not touched.)"
    )


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


async def _provider_usage(name: str) -> str:
    try:
        text = await get_provider(name).usage()
    except Exception as e:
        log.warning("usage for %s failed: %s", name, e)
        text = f"❌ {e}"
    return f"**{name}**\n```\n{text[:1700]}\n```"


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
    BindsTo/Wants); otherwise the process re-execs itself in place, keeping its PID.
    """
    unit = service.current_unit()
    log.warning("restarting harness (%s)", unit or "exec")
    if unit:
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
            rc, pip_out = await _run_cmd(
                sys.executable, "-m", "pip", "install", "-q", "-r", "requirements.txt"
            )
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

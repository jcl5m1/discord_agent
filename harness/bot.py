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
import logging
import re
import subprocess

import discord

from . import config
from .instances import Instance, InstanceManager
from .providers.base import get_provider
from .render import render_html_to_png

if config.PUBLIC_BASE_URL:
    from . import proxy

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
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


@client.event
async def setup_hook() -> None:
    if config.PUBLIC_BASE_URL:
        await proxy.start_proxy(store)


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
    ports_before = _listening_ports()

    buf = ""
    last_edit = 0.0
    error: str | None = None

    def display(text: str) -> str:
        text = text.strip()
        return text if len(text) <= DISCORD_MSG_LIMIT else text[: DISCORD_MSG_LIMIT - 20] + "\n…(truncated)"

    try:
        agen = provider.run_turn(inst.workdir, _with_brief(inst, prompt), inst.session_id)
        while True:
            try:
                event = await agen.__anext__()
            except StopAsyncIteration:
                break
            if event.kind == "text":
                buf += event.text
            elif event.kind == "status":
                log.info("[%s] %s", inst.name, event.text)
            elif event.kind == "error":
                error = event.text
            elif event.kind == "done":
                buf = event.text or buf
                if event.session_id and event.session_id != inst.session_id:
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
        await status_msg.edit(content=f"❌ {display(error)}")
        return

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


@client.event
async def on_ready() -> None:
    guild = discord.Object(id=config.GUILD_ID)
    tree.copy_global_to(guild=guild)
    await tree.sync(guild=guild)
    log.info("logged in as %s", client.user)
    for inst in store.all():
        asyncio.get_running_loop().create_task(_catch_up(inst))


@client.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot or not message.guild:
        return
    inst = store.get(message.channel.id)
    if inst is None:
        return
    await _process_user_message(inst, message)


async def _process_user_message(inst: Instance, message: discord.Message) -> None:
    try:
        async with _global_semaphore:
            async with _channel_lock(message.channel.id):
                async with message.channel.typing():
                    status_msg = await message.channel.send("🤔 …")
                    await _run_turn(inst, message.content, status_msg)
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
    if not inst.last_message_id:
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


@tree.command(name="new", description="Create a new agent instance channel")
@discord.app_commands.describe(
    name="Instance name (becomes the channel name)",
    workdir="Working directory for the agent (default: DEFAULT_WORKDIR)",
    prompt="Optional first prompt to start the agent with",
)
async def cmd_new(
    interaction: discord.Interaction, name: str, workdir: str | None = None, prompt: str | None = None
) -> None:
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
        store.put(existing)
        await interaction.followup.send(
            f"✅ reopened {channel.mention} — resuming session `{existing.session_id}` "
            f"on next message."
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
        provider=config.DEFAULT_PROVIDER,
        workdir=workdir or config.DEFAULT_WORKDIR,
    )
    inst.ensure_web_token()
    store.put(inst)
    await interaction.followup.send(
        f"✅ created {channel.mention} — just chat in it. "
        f"Provider: `{inst.provider}`, workdir: `{inst.workdir}`"
    )
    if prompt:
        await on_message_simple(channel, prompt)


async def on_message_simple(channel: discord.TextChannel, prompt: str) -> None:
    inst = store.get(channel.id)
    if inst is None:
        return
    async with _global_semaphore:
        async with _channel_lock(channel.id):
            async with channel.typing():
                status_msg = await channel.send("🤔 …")
                await _run_turn(inst, prompt, status_msg)


@tree.command(name="stop", description="Close an agent channel (session is kept for resume)")
@discord.app_commands.describe(name="Instance name to stop")
async def cmd_stop(interaction: discord.Interaction, name: str) -> None:
    await interaction.response.defer()
    inst = store.by_name(name)
    if not inst:
        await interaction.followup.send(f"❌ no instance named '{name}'")
        return
    channel = interaction.guild.get_channel(inst.channel_id) if interaction.guild else None
    if isinstance(channel, discord.TextChannel):
        try:
            await channel.set_permissions(interaction.guild.default_role, send_messages=False)
        except discord.Forbidden:
            await interaction.followup.send(
                f"⚠️ couldn't lock the channel — the bot lacks the **Manage Roles** "
                f"permission. Either re-invite it with that permission, or just ignore "
                f"the channel. Session for '{name}' is retained either way."
            )
            return
    await interaction.followup.send(
        f"✅ stopped '{name}' — channel locked, session retained. "
        f"Use `/new name:{name}` to reopen and resume."
    )


@tree.command(name="list", description="List all agent instances")
async def cmd_list(interaction: discord.Interaction) -> None:
    lines = [
        f"• **{i.name}** — `{i.provider}` @ `{i.workdir}`"
        + (f" (session: `{i.session_id}`)" if i.session_id else " (no session yet)")
        for i in store.all()
    ]
    await interaction.response.send_message("\n".join(lines) or "no instances yet")


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
    client.run(config.BOT_TOKEN)


if __name__ == "__main__":
    main()

"""WhatsApp bridge: one group chat per agent instance, mirrored with Discord.

The harness logs in as a linked device of a dedicated WhatsApp account (the
"bot number") via neonize/whatsmeow, creates a group "<name>-bot" per instance
with the owner (WHATSAPP_OWNER) in it, and relays traffic both ways:

- messages the owner sends in a group become turns for that instance, and are
  echoed into the Discord channel;
- messages typed in Discord, and every agent reply, are echoed into the group.

Pairing state lives in data/whatsapp.db; link once with /whatsapp in Discord.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Awaitable, Callable

import segno
from neonize.aioze.client import NewAClient
from neonize.aioze.events import ConnectedEv, LoggedOutEv, MessageEv, PairStatusEv
from neonize.utils import ChatPresence, ChatPresenceMedia, extract_text
from neonize.utils.jid import build_jid

from . import config
from .instances import Instance, InstanceManager

log = logging.getLogger(__name__)

GROUP_SERVER = "g.us"
# Pause between group creations so a burst at startup doesn't look like spam.
GROUP_CREATE_GAP = 5.0

# (instance, text, sender display name) -> handled by the bot
MessageHandler = Callable[[Instance, str, str], Awaitable[None]]


def group_name(inst: Instance) -> str:
    return f"{inst.name}-bot"


def to_whatsapp_markdown(text: str) -> str:
    """Adapt Discord markdown: **bold** -> *bold*, <url> -> url, ~~x~~ -> ~x~."""
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text, flags=re.DOTALL)
    text = re.sub(r"~~(.+?)~~", r"~\1~", text, flags=re.DOTALL)
    return re.sub(r"<(https?://[^>\s]+)>", r"\1", text)


class WhatsAppBridge:
    def __init__(self, store: InstanceManager, on_message: MessageHandler):
        self.store = store
        self.on_message = on_message
        self.client = NewAClient(str(config.DATA_DIR / "whatsapp.db"))
        self.ready = asyncio.Event()  # connected and logged in
        self.latest_qr: bytes | None = None
        self._seen: set[str] = set()  # message ids, guards against redelivery
        self._group_lock = asyncio.Lock()
        self._owner_lids: set[str] = set()  # LIDs resolved to the owner's number
        # Per-group send locks; asyncio locks are FIFO, so mirrored messages
        # land in the order they were queued even though sends are async tasks.
        self._send_locks: dict[str, asyncio.Lock] = {}

        self.client.qr(self._on_qr)
        self.client.event(ConnectedEv)(self._on_connected)
        self.client.event(PairStatusEv)(self._on_pair_status)
        self.client.event(LoggedOutEv)(self._on_logged_out)
        self.client.event(MessageEv)(self._on_message_event)

    # --- lifecycle ---

    async def start(self) -> None:
        await self.client.connect()
        log.info("whatsapp: connecting (owner %s)", config.WHATSAPP_OWNER)

    @property
    def linked(self) -> bool:
        return self.ready.is_set()

    def qr_png(self) -> bytes | None:
        """Current pairing QR as a PNG, or None if paired / not offered yet."""
        if self.linked or not self.latest_qr:
            return None
        import io

        buf = io.BytesIO()
        segno.make_qr(self.latest_qr).save(buf, kind="png", scale=10, border=4)
        return buf.getvalue()

    async def pair_code(self, phone: str) -> str:
        """Pairing code for 'Link with phone number' on the bot phone."""
        return await self.client.PairPhone(re.sub(r"\D", "", phone), show_push_notification=True)

    async def _on_qr(self, _client: NewAClient, data: bytes) -> None:
        if self.latest_qr is None:  # QR codes rotate every ~20s; say it once
            log.info("whatsapp: not linked — run /whatsapp in Discord to pair")
        self.latest_qr = data

    async def _on_pair_status(self, _client: NewAClient, ev: PairStatusEv) -> None:
        log.info("whatsapp: pair status %s %s", ev.Status, ev.Error or "")

    async def _on_connected(self, _client: NewAClient, _ev: ConnectedEv) -> None:
        self.latest_qr = None
        self.ready.set()
        log.info("whatsapp: connected")
        await self.ensure_all_groups()

    async def _on_logged_out(self, _client: NewAClient, ev: LoggedOutEv) -> None:
        self.ready.clear()
        log.warning("whatsapp: logged out (reason %s) — re-pair with /whatsapp", ev.Reason)

    # --- groups ---

    async def ensure_all_groups(self) -> None:
        for inst in self.store.all():
            if not inst.stopped and not inst.wa_group:
                if await self.ensure_group(inst):
                    await asyncio.sleep(GROUP_CREATE_GAP)

    async def ensure_group(self, inst: Instance) -> bool:
        """Create the instance's group (with the owner in it) if it has none.

        Returns True if a group was created.
        """
        if not self.linked or inst.wa_group:
            return False
        async with self._group_lock:
            if inst.wa_group:
                return False
            owner = build_jid(re.sub(r"\D", "", config.WHATSAPP_OWNER))
            try:
                info = await self.client.create_group(group_name(inst), [owner])
            except Exception:
                log.exception("whatsapp: could not create group for %s", inst.name)
                return False
            inst.wa_group = info.JID.User
            self.store.put(inst)
            log.info("whatsapp: created group %r (%s)", group_name(inst), inst.wa_group)
            # The owner's privacy settings may block direct adds; fall back to an invite.
            if not any(p.PhoneNumber.User == owner.User or p.JID.User == owner.User
                       for p in info.Participants if not p.Error):
                try:
                    link = await self.client.get_group_invite_link(self._jid(inst))
                    await self.client.send_message(
                        owner, f"Join *{group_name(inst)}* to chat with that agent: {link}"
                    )
                    log.info("whatsapp: owner not added directly; sent invite for %s", inst.name)
                except Exception:
                    log.exception("whatsapp: could not send invite for %s", inst.name)
            return True

    async def leave_group(self, inst: Instance) -> None:
        if not self.linked or not inst.wa_group:
            return
        try:
            await self.client.leave_group(self._jid(inst))
        except Exception:
            log.exception("whatsapp: could not leave group for %s", inst.name)

    # --- outbound ---

    def _jid(self, inst: Instance):
        return build_jid(inst.wa_group, GROUP_SERVER)

    def _send_lock(self, inst: Instance) -> asyncio.Lock:
        return self._send_locks.setdefault(inst.wa_group, asyncio.Lock())

    async def send_text(self, inst: Instance, text: str) -> None:
        if not self.linked or not inst.wa_group or not text.strip():
            return
        try:
            async with self._send_lock(inst):
                await self.client.send_message(self._jid(inst), to_whatsapp_markdown(text))
        except Exception:
            log.exception("whatsapp: send failed for %s", inst.name)

    async def send_image(self, inst: Instance, png: bytes, caption: str) -> None:
        if not self.linked or not inst.wa_group:
            return
        try:
            async with self._send_lock(inst):
                await self.client.send_image(
                    self._jid(inst), png, caption=to_whatsapp_markdown(caption)
                )
        except Exception:
            log.exception("whatsapp: image send failed for %s", inst.name)

    async def send_document(self, inst: Instance, data: bytes, filename: str, mimetype: str) -> None:
        if not self.linked or not inst.wa_group:
            return
        try:
            async with self._send_lock(inst):
                await self.client.send_document(
                    self._jid(inst), data, filename=filename, title=filename, mimetype=mimetype
                )
        except Exception:
            log.exception("whatsapp: document send failed for %s", inst.name)

    async def typing(self, inst: Instance, on: bool = True) -> None:
        if not self.linked or not inst.wa_group:
            return
        state = ChatPresence.CHAT_PRESENCE_COMPOSING if on else ChatPresence.CHAT_PRESENCE_PAUSED
        try:
            await self.client.send_chat_presence(
                self._jid(inst), state, ChatPresenceMedia.CHAT_PRESENCE_MEDIA_TEXT
            )
        except Exception:
            log.debug("whatsapp: presence failed for %s", inst.name, exc_info=True)

    # --- inbound ---

    async def _is_owner(self, src) -> bool:
        owner = re.sub(r"\D", "", config.WHATSAPP_OWNER)
        if owner in (src.Sender.User, src.SenderAlt.User):
            return True
        if src.Sender.Server != "lid":
            return False
        if src.Sender.User in self._owner_lids:
            return True
        try:
            pn = await self.client.get_pn_from_lid(src.Sender)
        except Exception:
            log.debug("whatsapp: LID lookup failed for %s", src.Sender.User, exc_info=True)
            return False
        if pn.User == owner:
            self._owner_lids.add(src.Sender.User)
            return True
        return False

    async def _on_message_event(self, _client: NewAClient, ev: MessageEv) -> None:
        src = ev.Info.MessageSource
        # Our own sends (including mirrored Discord traffic) come back as IsFromMe.
        if src.IsFromMe or not src.IsGroup or ev.Info.ID in self._seen:
            return
        inst = next((i for i in self.store.all() if i.wa_group == src.Chat.User), None)
        if inst is None:
            return
        if not await self._is_owner(src):
            # Agents run with full tool access, so only the owner may drive them.
            log.warning("[%s] ignoring whatsapp message from non-owner %s", inst.name, src.Sender.User)
            return
        text = extract_text(ev.Message).strip()
        if not text:
            await self.send_text(inst, "⚠️ only text messages are forwarded to the agent")
            return
        self._seen.add(ev.Info.ID)
        sender = ev.Info.Pushname or "WhatsApp"
        log.info("[%s] whatsapp message from %s: %.160s", inst.name, sender, text)
        # Handle off the event callback so a long turn doesn't stall other events.
        asyncio.get_running_loop().create_task(self.on_message(inst, text, sender))

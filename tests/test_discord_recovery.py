"""Discord I/O recovery must not strand turns or hold message locks.

    .venv/bin/python -m unittest tests.test_discord_recovery
"""

import asyncio
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import discord

from harness import bot, config, discord_markdown, limits, resume
from harness.instances import Instance, InstanceManager
from harness.pending import PendingTurns
from harness.providers.base import StreamEvent
from tests.test_fallback import FakeStatus, ScriptedProvider, done


def http_429():
    response = SimpleNamespace(status=429, reason="Too Many Requests")
    return discord.HTTPException(response, {"message": "rate limited"})


async def discord_timeout(**kwargs):
    await asyncio.Event().wait()


class RecoveryStatus(FakeStatus):
    def __init__(self, channel, on_edit=None):
        super().__init__(channel)
        self.on_edit = on_edit
        self.edit_attempts = []

    async def edit(self, **kwargs):
        self.edit_attempts.append(kwargs)
        if self.on_edit is not None:
            await self.on_edit(kwargs)
        self.edits.append(kwargs)


class RecoveryChannel:
    def __init__(self, channel_id=17, status_handlers=(), on_send=None):
        self.id = channel_id
        self.status_handlers = list(status_handlers)
        self.on_send = on_send
        self.sent = []
        self.send_attempts = []
        self.topics = []
        self.statuses = []

    @asynccontextmanager
    async def typing(self):
        yield

    async def send(self, content=None, **kwargs):
        self.send_attempts.append((content, kwargs))
        if self.on_send is not None:
            await self.on_send(content, kwargs)
        self.sent.append((content, kwargs))
        if content == "🤔 …":
            handler = self.status_handlers.pop(0) if self.status_handlers else None
            status = RecoveryStatus(self, handler)
            self.statuses.append(status)
            return status
        return FakeStatus(self)

    async def edit(self, **kwargs):
        self.topics.append(kwargs)


class RecoveryMessage:
    def __init__(self, channel, message_id, content):
        self.channel = channel
        self.id = message_id
        self.content = content
        self.author = SimpleNamespace(display_name="tester", bot=False)
        self.reactions = []

    async def add_reaction(self, emoji):
        self.reactions.append(emoji)


class GatedProvider(ScriptedProvider):
    def __init__(self, *turns):
        super().__init__(*turns)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run_turn(self, workdir, prompt, session_id=None, model=None, **extra):
        self.calls.append({
            "workdir": workdir,
            "prompt": prompt,
            "session_id": session_id,
            "model": model,
            "extra": extra,
        })
        if len(self.calls) == 1:
            self.started.set()
            await self.release.wait()
        for event in self.turns.pop(0):
            yield event


class DiscordRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.semaphore = asyncio.Semaphore(1)
        self.reply_retries = set()
        patches = [
            mock.patch.object(bot, "store", InstanceManager(self.root / "instances.json")),
            mock.patch.object(bot, "pending", PendingTurns()),
            mock.patch.object(bot, "_global_semaphore", self.semaphore),
            mock.patch.object(bot, "_reply_retries", self.reply_retries),
            mock.patch.object(limits, "PATH", self.root / "usage_limit.json"),
            mock.patch.object(resume, "PATH", self.root / "interrupted_turns.json"),
            mock.patch.object(config, "AUTO_FALLBACK", False),
            mock.patch.object(config, "PUBLIC_BASE_URL", None),
            mock.patch.object(config, "SEND_DELAY", 0),
            mock.patch.object(bot, "worklog", None),
            mock.patch.object(bot, "wa", None),
            mock.patch.object(bot, "_harness_brief", return_value="[test brief]\n"),
            mock.patch.object(bot, "_listening_ports", return_value=set()),
            mock.patch.object(bot, "DISCORD_CALL_TIMEOUT", 0.01),
            mock.patch.object(bot, "DISCORD_REPLY_TIMEOUT", 0.01),
            mock.patch.object(bot, "DISCORD_REPLY_RETRIES", 1),
            mock.patch.object(bot, "DISCORD_REPLY_RETRY_DELAY", 0.001),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        for target in (
            bot._active_turns,
            bot._turn_ended,
            bot._running_message,
            bot._disregard_notes,
            bot._resume_counts,
            bot._channel_locks,
        ):
            patch = mock.patch.dict(target, {}, clear=True)
            patch.start()
            self.addCleanup(patch.stop)

    async def asyncTearDown(self):
        tasks = list(self.reply_retries)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def instance(self):
        inst = Instance(
            channel_id=17,
            name="recovery-agent",
            provider="claude",
            workdir=str(self.root),
        )
        bot.store.put(inst)
        return inst

    async def drain_reply_retries(self):
        while self.reply_retries:
            await asyncio.wait_for(
                asyncio.gather(*list(self.reply_retries), return_exceptions=True), 1
            )

    async def run_first_and_queued_message(self, status_handler):
        inst = self.instance()
        provider = GatedProvider(done("first reply", "one"), done("second reply", "two"))
        channel = RecoveryChannel(status_handlers=[status_handler])
        messages = [
            RecoveryMessage(channel, 101, "first request"),
            RecoveryMessage(channel, 102, "second request"),
        ]
        with mock.patch.object(bot, "get_provider", return_value=provider):
            first = asyncio.create_task(bot._process_user_message(inst, messages[0]))
            await asyncio.wait_for(provider.started.wait(), 1)
            second = asyncio.create_task(bot._process_user_message(inst, messages[1]))
            await asyncio.sleep(0)
            self.assertFalse(second.done(), "the second message should wait for the channel lock")
            provider.release.set()
            await asyncio.wait_for(asyncio.gather(first, second), 1)
        await self.drain_reply_retries()

        self.assertEqual(len(provider.calls), 2)
        self.assertIn("first request", provider.calls[0]["prompt"])
        self.assertIn("second request", provider.calls[1]["prompt"])
        self.assertNotIn(inst.channel_id, bot._active_turns)
        self.assertFalse(bot._channel_lock(inst.channel_id).locked())
        self.assertEqual(self.semaphore._value, 1)
        self.assertEqual(messages[0].reactions, ["✅"])
        self.assertEqual(messages[1].reactions, ["✅"])
        self.assertEqual(channel.topics, [])
        return channel

    async def test_initial_stop_timeout_cleans_turn_and_releases_queued_message(self):
        async def fail_stop_edit(kwargs):
            if kwargs.get("view") is not None:
                await discord_timeout()

        channel = await self.run_first_and_queued_message(fail_stop_edit)
        self.assertEqual(channel.statuses[0].edits[-1]["content"], "first reply")
        self.assertEqual(channel.statuses[1].edits[-1]["content"], "second reply")

    async def test_initial_stop_http_429_cleans_turn_and_releases_queued_message(self):
        async def fail_stop_edit(kwargs):
            if kwargs.get("view") is not None:
                raise http_429()

        channel = await self.run_first_and_queued_message(fail_stop_edit)
        self.assertEqual(channel.statuses[0].edits[-1]["content"], "first reply")
        self.assertEqual(channel.statuses[1].edits[-1]["content"], "second reply")

    async def test_final_timeout_cleans_turn_and_releases_queued_message(self):
        async def fail_final_edit(kwargs):
            if "view" in kwargs and kwargs["view"] is None:
                await discord_timeout()

        channel = await self.run_first_and_queued_message(fail_final_edit)
        final_attempts = [
            kwargs for kwargs in channel.statuses[0].edit_attempts
            if "view" in kwargs and kwargs["view"] is None
        ]
        self.assertEqual(len(final_attempts), 2, "the original edit and its retry both time out")
        self.assertEqual(channel.statuses[1].edits[-1]["content"], "second reply")

    async def test_final_http_429_cleans_turn_and_releases_queued_message(self):
        async def fail_final_edit(kwargs):
            if "view" in kwargs and kwargs["view"] is None:
                raise http_429()

        channel = await self.run_first_and_queued_message(fail_final_edit)
        final_attempts = [
            kwargs for kwargs in channel.statuses[0].edit_attempts
            if "view" in kwargs and kwargs["view"] is None
        ]
        self.assertEqual(len(final_attempts), 2, "the original edit and its retry both get 429")
        self.assertEqual(channel.statuses[1].edits[-1]["content"], "second reply")

    async def test_streaming_http_429_is_not_a_provider_failure(self):
        inst = self.instance()
        provider = ScriptedProvider([
            StreamEvent("text", text="partial reply"),
            StreamEvent("done", text="complete reply", session_id="session"),
        ])

        async def fail_streaming_edit(kwargs):
            if "view" not in kwargs and kwargs.get("content") == "partial reply":
                raise http_429()

        status = RecoveryStatus(RecoveryChannel(), fail_streaming_edit)
        with mock.patch.object(bot, "get_provider", return_value=provider):
            result = await bot._run_turn(inst, "stream this", status)

        self.assertEqual(result, "done")
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(status.edits[-1]["content"], "complete reply")
        self.assertNotIn(inst.channel_id, bot._active_turns)
        self.assertEqual(self.reply_retries, set())

    async def test_final_retry_is_supervised_and_succeeds_after_locks_release(self):
        inst = self.instance()
        provider = ScriptedProvider(done("essential reply", "session"))
        retry_started = asyncio.Event()
        allow_retry = asyncio.Event()
        retry_state = []
        final_attempts = 0

        async def fail_once_then_gate(kwargs):
            nonlocal final_attempts
            if "view" not in kwargs or kwargs["view"] is not None:
                return
            final_attempts += 1
            if final_attempts == 1:
                raise http_429()
            retry_state.append((
                bot._channel_lock(inst.channel_id).locked(),
                self.semaphore._value,
                inst.channel_id in bot._active_turns,
                asyncio.current_task() in self.reply_retries,
            ))
            retry_started.set()
            await allow_retry.wait()

        channel = RecoveryChannel(status_handlers=[fail_once_then_gate])
        with mock.patch.object(bot, "get_provider", return_value=provider):
            await bot.on_message_simple(channel, "deliver this")
            await asyncio.wait_for(retry_started.wait(), 1)
            self.assertEqual(len(self.reply_retries), 1)
            self.assertEqual(retry_state, [(False, 1, False, True)])
            allow_retry.set()
            await self.drain_reply_retries()

        self.assertEqual(channel.statuses[0].edits[-1]["content"], "essential reply")
        self.assertNotIn(inst.channel_id, bot._active_turns)
        self.assertFalse(bot._channel_lock(inst.channel_id).locked())
        self.assertEqual(self.semaphore._value, 1)

    async def test_extra_chunk_failure_retries_only_undelivered_chunks(self):
        inst = self.instance()
        reply = "A" * 1500 + "\n\n" + "B" * 1500 + "\n\n" + "C" * 1500 + "\n\n" + "D" * 100
        chunks = discord_markdown.split_message(reply, bot.DISCORD_MSG_LIMIT)
        self.assertEqual(len(chunks), 3)
        provider = ScriptedProvider(done(reply, "session"))
        failed_once = False

        async def fail_last_chunk_once(content, kwargs):
            nonlocal failed_once
            if content == chunks[2] and not failed_once:
                failed_once = True
                raise http_429()

        channel = RecoveryChannel(on_send=fail_last_chunk_once)
        with mock.patch.object(bot, "get_provider", return_value=provider):
            await bot.on_message_simple(channel, "send three chunks")
        await self.drain_reply_retries()

        sent_text = [content for content, kwargs in channel.sent]
        attempted_text = [content for content, kwargs in channel.send_attempts]
        self.assertEqual(sent_text.count(chunks[1]), 1)
        self.assertEqual(sent_text.count(chunks[2]), 1)
        self.assertEqual(attempted_text.count(chunks[1]), 1, "a delivered chunk must not be resent")
        self.assertEqual(attempted_text.count(chunks[2]), 2)
        self.assertNotIn(inst.channel_id, bot._active_turns)
        self.assertFalse(bot._channel_lock(inst.channel_id).locked())

    async def test_retry_preserves_chunk_progress_and_attachments(self):
        channel = RecoveryChannel()
        status = RecoveryStatus(channel)
        failed = False

        async def fail_c_once(content, kwargs):
            nonlocal failed
            if content == "C" and not failed:
                failed = True
                raise http_429()

        channel.on_send = fail_c_once
        attachments = {0: [(b"image", "reply.png")], 1: [(b"table", "table-1.png")]}
        with mock.patch.object(bot, "DISCORD_REPLY_RETRIES", 2):
            await bot._retry_reply(
                status, ["A", "B", "C"], [1, 2], attachments, (b"html", "reply.html")
            )

        attempted = [content for content, kwargs in channel.send_attempts]
        self.assertEqual(attempted.count("B"), 1)
        self.assertEqual(attempted.count("C"), 2)
        self.assertEqual(status.edit_attempts[0]["attachments"][0].fp.read(), b"image")
        table_send = next(kwargs for content, kwargs in channel.sent if content == "B")
        self.assertEqual(table_send["files"][0].fp.read(), b"table")
        source_send = next(kwargs for content, kwargs in channel.sent if content is None)
        self.assertEqual(source_send["file"].fp.read(), b"html")

    async def test_cancelled_semaphore_reacquisition_still_cleans_active_turn(self):
        inst = self.instance()
        background_started = asyncio.Event()
        finish_provider = asyncio.Event()

        class BackgroundProvider(ScriptedProvider):
            async def run_turn(self, workdir, prompt, session_id=None, model=None, **extra):
                self.calls.append({"prompt": prompt})
                yield StreamEvent("background", text="1")
                background_started.set()
                await finish_provider.wait()
                yield StreamEvent("done", text="finished", session_id="session")

        provider = BackgroundProvider()
        status = RecoveryStatus(RecoveryChannel())
        await self.semaphore.acquire()
        with mock.patch.object(bot, "get_provider", return_value=provider):
            task = asyncio.create_task(bot._run_turn(inst, "background work", status))
            await asyncio.wait_for(background_started.wait(), 1)
            self.assertEqual(self.semaphore._value, 1)
            await self.semaphore.acquire()
            finish_provider.set()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertFalse(task.done(), "the turn should be waiting to reacquire its slot")
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.semaphore.release()

        self.assertNotIn(inst.channel_id, bot._active_turns)
        self.assertIn(inst.channel_id, bot._turn_ended)
        self.assertEqual(self.semaphore._value, 1)


if __name__ == "__main__":
    unittest.main()

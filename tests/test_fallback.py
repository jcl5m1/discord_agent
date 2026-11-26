"""Automatic Claude-to-secondary failover and session handoff.

    .venv/bin/python -m unittest tests.test_fallback
"""

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from harness import bot, config, limits, resume
from harness.instances import Instance, InstanceManager
from harness.providers.base import StreamEvent, TurnControl


class FakeChannel:
    def __init__(self):
        self.sent = []
        self.topics = []

    async def send(self, content=None, **kwargs):
        self.sent.append((content, kwargs))
        return FakeStatus(self)

    async def edit(self, **kwargs):
        self.topics.append(kwargs)


class FakeStatus:
    def __init__(self, channel=None):
        self.channel = channel or FakeChannel()
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


class ScriptedProvider:
    supports_graceful_interrupt = False
    supports_live_input = False

    def __init__(self, *turns, on_turn=None):
        self.turns = [list(turn) for turn in turns]
        self.calls = []
        self.on_turn = on_turn

    def resolve_model(self, model):
        return model

    async def run_turn(self, workdir, prompt, session_id=None, model=None, **extra):
        self.calls.append({
            "workdir": workdir,
            "prompt": prompt,
            "session_id": session_id,
            "model": model,
            "extra": extra,
        })
        if self.on_turn:
            self.on_turn(self.calls[-1], extra)
        if not self.turns:
            raise AssertionError("unexpected provider turn")
        for event in self.turns.pop(0):
            yield event


def done(text, session_id):
    return [StreamEvent("done", text=text, session_id=session_id)]


def limited(reset_at, session_id="claude-session"):
    return [
        StreamEvent("session", session_id=session_id),
        StreamEvent("limit", text="usage limit reached", session_id=session_id, reset_at=reset_at),
    ]


class FallbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        patches = [
            mock.patch.object(bot, "store", InstanceManager(self.root / "instances.json")),
            mock.patch.object(limits, "PATH", self.root / "usage_limit.json"),
            mock.patch.object(resume, "PATH", self.root / "interrupted_turns.json"),
            mock.patch.object(config, "AUTO_FALLBACK", True),
            mock.patch.object(config, "SECONDARY_PROVIDER", "kimi"),
            mock.patch.object(config, "SECONDARY_MODEL", "kimi-test"),
            mock.patch.object(config, "PUBLIC_BASE_URL", None),
            mock.patch.object(bot, "worklog", None),
            mock.patch.object(bot, "wa", None),
            mock.patch.object(bot, "_harness_brief", return_value="[test brief]\n"),
            mock.patch.object(bot, "_listening_ports", return_value=set()),
            mock.patch.object(bot, "_start_limit_resumer"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        for target in (bot._active_turns, bot._turn_ended, bot._running_message,
                       bot._disregard_notes, bot._resume_counts):
            patch = mock.patch.dict(target, {}, clear=True)
            patch.start()
            self.addCleanup(patch.stop)

    def instance(self, **kwargs):
        inst = Instance(
            channel_id=17,
            name="fallback-agent",
            provider="claude",
            workdir=str(self.root),
            **kwargs,
        )
        bot.store.put(inst)
        return inst

    async def recover(self, inst, providers, prompt="build the feature"):
        status = FakeStatus()
        with mock.patch.object(bot, "get_provider", side_effect=providers.__getitem__):
            await bot._run_turn_recovering(inst, prompt, status)
        return status

    async def test_limit_event_immediately_fails_over_without_cutoff_record(self):
        reset_at = time.time() + 3600
        inst = self.instance()
        claude = ScriptedProvider(limited(reset_at))
        kimi = ScriptedProvider(done("secondary reply", "kimi-session"))

        await self.recover(inst, {"claude": claude, "kimi": kimi})

        self.assertEqual(len(claude.calls), 1)
        self.assertEqual(len(kimi.calls), 1)
        self.assertIn("[automatic model fallback]", kimi.calls[0]["prompt"])
        self.assertIn("build the feature", kimi.calls[0]["prompt"])
        self.assertEqual(kimi.calls[0]["model"], "kimi-test")
        self.assertIsNotNone(limits.resume_at())
        self.assertFalse(limits.pending())
        self.assertIn("Secondary request:\nbuild the feature", inst.fallback_handoff)
        self.assertIn("Secondary reply:\nsecondary reply", inst.fallback_handoff)

    async def test_primary_and_secondary_sessions_are_captured_distinctly(self):
        inst = self.instance(session_id="claude-original")
        claude = ScriptedProvider(done("primary reply", "claude-new"))
        kimi = ScriptedProvider(done("secondary reply", "kimi-new"))
        status = FakeStatus()
        with mock.patch.object(bot, "get_provider", side_effect={"claude": claude, "kimi": kimi}.__getitem__):
            await bot._run_turn(inst, "primary request", status)
            await bot._run_turn(inst, "secondary request", status, use_fallback=True)

        self.assertEqual(inst.session_id, "claude-new")
        self.assertEqual(inst.secondary_session_id, "kimi-new")
        self.assertEqual(claude.calls[0]["session_id"], "claude-original")
        self.assertIsNone(kimi.calls[0]["session_id"])

    async def test_active_limit_routes_to_existing_secondary_session(self):
        now = time.time()
        reset_at = now + 3600
        inst = self.instance(session_id="claude-session", secondary_session_id="kimi-existing")
        expected_resume = limits.hit(reset_at, inst.channel_id, inst.name, "old", now=now, record=False)
        claude = ScriptedProvider()
        kimi = ScriptedProvider(done("continued", "kimi-existing"))

        await self.recover(inst, {"claude": claude, "kimi": kimi}, "next request")

        self.assertEqual(claude.calls, [])
        self.assertEqual(kimi.calls[0]["session_id"], "kimi-existing")
        self.assertIn("next request", kimi.calls[0]["prompt"])
        self.assertEqual(limits.resume_at(), expected_resume)

    async def test_primary_receives_handoff_after_reset_and_clears_it(self):
        now = time.time()
        limits.hit(now - 120, 17, "fallback-agent", "old", now=now, record=False)
        inst = self.instance(
            session_id="claude-session",
            secondary_session_id="kimi-session",
            fallback_handoff="Secondary request:\nold\n\nSecondary reply:\nfinished old work",
        )
        claude = ScriptedProvider(done("primary resumed", "claude-session"))
        kimi = ScriptedProvider()

        await self.recover(inst, {"claude": claude, "kimi": kimi}, "current request")

        self.assertEqual(kimi.calls, [])
        self.assertEqual(len(claude.calls), 1)
        self.assertIn("[model fallback]", claude.calls[0]["prompt"])
        self.assertIn("finished old work", claude.calls[0]["prompt"])
        self.assertIn("The user's current request:\ncurrent request", claude.calls[0]["prompt"])
        self.assertIsNone(inst.fallback_handoff)
        self.assertEqual(inst.session_id, "claude-session")
        self.assertEqual(inst.secondary_session_id, "kimi-session")

    async def test_secondary_off_disables_immediate_fallback(self):
        inst = self.instance(secondary_provider="")
        claude = ScriptedProvider(limited(time.time() + 3600))
        kimi = ScriptedProvider(done("must not run", "kimi-session"))

        await self.recover(inst, {"claude": claude, "kimi": kimi})

        self.assertEqual(len(claude.calls), 1)
        self.assertEqual(kimi.calls, [])
        self.assertIsNone(inst.secondary_session_id)
        self.assertIsNone(inst.fallback_handoff)
        self.assertTrue(limits.pending())
        self.assertIn("build the feature", limits.take(inst.channel_id, clear_gate=False)[17]["prompt"])

    def test_secondary_model_selection_can_change_provider(self):
        self.assertEqual(
            bot._parse_secondary("claude", "kimi", "muse:spark-1.3"),
            ("muse", "spark-1.3"),
        )
        self.assertEqual(bot._parse_secondary("claude", "kimi", "k3"), ("kimi", "k3"))
        self.assertEqual(bot._parse_secondary("claude", "kimi", "off"), ("", None))
        with self.assertRaises(ValueError):
            bot._parse_secondary("claude", "kimi", "claude:opus")

    def test_secondary_provider_change_resets_only_secondary_session(self):
        inst = self.instance(
            session_id="claude-session",
            brief_version=6,
            worklog_briefed=True,
            secondary_session_id="kimi-session",
            secondary_brief_version=6,
            secondary_worklog_briefed=True,
            fallback_handoff="keep this handoff",
        )

        bot._set_secondary(inst, ("kimi", "another-kimi-model"))
        self.assertEqual(inst.secondary_session_id, "kimi-session")
        self.assertEqual(inst.secondary_brief_version, 6)
        self.assertTrue(inst.secondary_worklog_briefed)

        bot._set_secondary(inst, ("codex", "codex-model"))
        self.assertIsNone(inst.secondary_session_id)
        self.assertEqual(inst.secondary_brief_version, 0)
        self.assertFalse(inst.secondary_worklog_briefed)
        self.assertEqual(inst.session_id, "claude-session")
        self.assertEqual(inst.brief_version, 6)
        self.assertTrue(inst.worklog_briefed)
        self.assertEqual(inst.fallback_handoff, "keep this handoff")

    async def test_sticky_fallback_consumes_cutoff_prompt_once_and_keeps_gate(self):
        now = time.time()
        inst = self.instance(secondary_session_id="kimi-session")
        expected_resume = limits.hit(
            now + 3600, inst.channel_id, inst.name, "original cutoff work", now=now
        )
        claude = ScriptedProvider()
        kimi = ScriptedProvider(
            done("first continuation", "kimi-session"),
            done("second continuation", "kimi-session"),
        )

        await self.recover(inst, {"claude": claude, "kimi": kimi}, "new request")
        await self.recover(inst, {"claude": claude, "kimi": kimi}, "later request")

        self.assertEqual(claude.calls, [])
        self.assertIn("original cutoff work", kimi.calls[0]["prompt"])
        self.assertIn("new request", kimi.calls[0]["prompt"])
        self.assertNotIn("original cutoff work", kimi.calls[1]["prompt"])
        self.assertNotIn("An earlier Claude turn was cut off", kimi.calls[1]["prompt"])
        self.assertIn("later request", kimi.calls[1]["prompt"])
        self.assertFalse(limits.pending())
        self.assertEqual(limits.resume_at(), expected_resume)

    async def test_failover_preserves_disregard_note_and_ordered_live_input(self):
        inst = self.instance()
        bot._disregard_notes[inst.channel_id] = "The deleted draft asked for something else"

        def send_live_input(call, extra):
            control = extra["control"]
            delivered = control.send("first correction")
            control.take_held()
            edited = control.send("rough correction")
            self.assertTrue(control.edit(edited, "edited correction"))
            withdrawn = control.send("withdrawn correction")
            self.assertTrue(control.withdraw(withdrawn))
            unread = control.send("final correction")
            turn = bot._active_turns[inst.channel_id]
            turn["steered"][edited] = mock.MagicMock()
            turn["steered"][unread] = mock.MagicMock()

        claude = ScriptedProvider(limited(time.time() + 3600), on_turn=send_live_input)
        claude.supports_graceful_interrupt = True
        claude.supports_live_input = True
        kimi = ScriptedProvider(done("secondary reply", "kimi-session"))
        queued = mock.AsyncMock()
        with mock.patch.object(bot, "_process_user_message", new=queued):
            await self.recover(inst, {"claude": claude, "kimi": kimi})

        prompt = kimi.calls[0]["prompt"]
        ordered = [
            "The deleted draft asked for something else",
            "build the feature",
            "Additional user messages sent during the primary turn, in order:",
            "first correction",
            "edited correction",
            "final correction",
        ]
        positions = [prompt.index(text) for text in ordered]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("rough correction", prompt)
        self.assertNotIn("withdrawn correction", prompt)
        queued.assert_not_awaited()

    async def test_secondary_error_handoff_replaces_pending_with_partial_work(self):
        inst = self.instance()
        observed = []
        kimi = ScriptedProvider(
            [
                StreamEvent("text", text="partial reply"),
                StreamEvent("status", text="ran a tool"),
                StreamEvent("error", text="provider failed"),
            ],
            on_turn=lambda call, extra: observed.append(inst.fallback_handoff),
        )
        status = FakeStatus()
        with mock.patch.object(bot, "get_provider", return_value=kimi):
            result = await bot._run_turn(inst, "secondary work", status, use_fallback=True)

        self.assertEqual(result, "error")
        self.assertIn("Secondary outcome: started", observed[0])
        self.assertIn("Secondary reply:\n(pending)", observed[0])
        self.assertNotIn("Secondary outcome: started", inst.fallback_handoff)
        self.assertIn("Secondary outcome: error; 1 tool events", inst.fallback_handoff)
        self.assertIn("Secondary reply:\npartial reply", inst.fallback_handoff)

    async def test_interrupted_secondary_handoff_preserves_partial_work(self):
        inst = self.instance()
        kimi = ScriptedProvider([
            StreamEvent("text", text="unfinished reply"),
            StreamEvent("status", text="ran a tool"),
            StreamEvent("interrupted", session_id="kimi-session"),
        ])
        status = FakeStatus()
        with mock.patch.object(bot, "get_provider", return_value=kimi):
            result = await bot._run_turn(inst, "secondary work", status, use_fallback=True)

        self.assertEqual(result, "interrupted")
        self.assertIn("Secondary outcome: interrupted; 1 tool events", inst.fallback_handoff)
        self.assertIn("Secondary reply:\nunfinished reply", inst.fallback_handoff)

    async def test_cancelled_secondary_handoff_preserves_partial_work(self):
        inst = self.instance()
        kimi = ScriptedProvider([
            StreamEvent("status", text="ran a tool"),
            StreamEvent("text", text="partial before restart"),
            StreamEvent("done", text="too late", session_id="kimi-session"),
        ])
        status = FakeStatus()
        editing = asyncio.Event()
        original_edit = status.edit

        async def block_partial_edit(**kwargs):
            if kwargs.get("content") == "partial before restart":
                editing.set()
                await asyncio.Event().wait()
            await original_edit(**kwargs)

        status.edit = block_partial_edit
        with mock.patch.object(bot, "get_provider", return_value=kimi):
            task = asyncio.create_task(
                bot._run_turn(inst, "secondary work", status, use_fallback=True)
            )
            await asyncio.wait_for(editing.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertIn(
            "Secondary outcome: interrupted by harness restart; 1 tool events",
            inst.fallback_handoff,
        )
        self.assertIn("Secondary reply:\npartial before restart", inst.fallback_handoff)

    async def test_fresh_reopen_clears_sessions_and_fallback_handoff(self):
        inst = self.instance(
            session_id="claude-session",
            secondary_session_id="kimi-session",
            fallback_handoff="unfinished secondary work",
        )
        channel = mock.MagicMock(id=99, name="fallback-agent", mention="#fallback-agent")
        guild = mock.MagicMock()
        guild.create_text_channel = mock.AsyncMock(return_value=channel)

        reopened, message = await bot._reopen_instance(guild, inst, fresh=True)

        self.assertIs(reopened, channel)
        self.assertIn("fresh `claude` session", message)
        self.assertIsNone(inst.session_id)
        self.assertIsNone(inst.secondary_session_id)
        self.assertIsNone(inst.fallback_handoff)
        self.assertIs(bot.store.get(99), inst)

    def test_secondary_on_rejects_empty_or_same_primary_config(self):
        with mock.patch.object(config, "SECONDARY_PROVIDER", ""):
            with self.assertRaisesRegex(ValueError, "not configured"):
                bot._parse_secondary("claude", None, "on")
        with mock.patch.object(config, "SECONDARY_PROVIDER", "claude"):
            with self.assertRaisesRegex(ValueError, "must differ"):
                bot._parse_secondary("claude", None, "on")
        with mock.patch.object(config, "SECONDARY_PROVIDER", "kimi"), \
             mock.patch.object(config, "SECONDARY_MODEL", "configured-model"):
            self.assertEqual(
                bot._parse_secondary("claude", None, "on"),
                ("kimi", "configured-model"),
            )

    def test_turn_control_history_tracks_edits_and_withdrawals_in_order(self):
        control = TurnControl()
        delivered = control.send("delivered original")
        control.take_held()
        edited = control.send("held typo")
        withdrawn = control.send("held retraction")
        unread = control.send("held final")

        self.assertFalse(control.edit(delivered, "too late"))
        self.assertFalse(control.withdraw(delivered))
        self.assertTrue(control.edit(edited, "held correction"))
        self.assertTrue(control.withdraw(withdrawn))
        self.assertEqual(
            list(control.messages.values()),
            ["delivered original", "held correction", "held final"],
        )


if __name__ == "__main__":
    unittest.main()

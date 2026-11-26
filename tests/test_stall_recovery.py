"""Provider stalls recover once, preserve input, and clean up every exit.

    .venv/bin/python -m unittest tests.test_stall_recovery
"""

import asyncio
import os
import signal
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

from harness import bot, config, limits, resume
from harness.instances import Instance, InstanceManager
from harness.providers.base import StreamEvent
from tests.test_fallback import FakeStatus, ScriptedProvider, done, limited


class StatusChannel:
    def __init__(self):
        self.sent = []
        self.statuses = []

    async def send(self, content=None, **kwargs):
        self.sent.append((content, kwargs))
        status = FakeStatus(self)
        self.statuses.append(status)
        return status


class TurnScriptProvider(ScriptedProvider):
    """A ScriptedProvider whose turns can also wait on gates or timers."""

    def __init__(self, *turns, on_turn=None):
        super().__init__(*turns, on_turn=on_turn)
        self.started = asyncio.Event()
        self.closed = 0

    async def run_turn(self, workdir, prompt, session_id=None, model=None, **extra):
        self.calls.append({
            "workdir": workdir,
            "prompt": prompt,
            "session_id": session_id,
            "model": model,
            "extra": extra,
        })
        self.started.set()
        if self.on_turn:
            self.on_turn(self.calls[-1], extra)
        if not self.turns:
            raise AssertionError("unexpected provider turn")
        try:
            for step in self.turns.pop(0):
                if isinstance(step, StreamEvent):
                    yield step
                else:
                    await step
        finally:
            self.closed += 1


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


class StallRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.semaphore = asyncio.Semaphore(1)
        self.spawned_pids = set()
        self.addCleanup(self.kill_spawned)
        patches = [
            mock.patch.object(bot, "store", InstanceManager(self.root / "instances.json")),
            mock.patch.object(bot, "_global_semaphore", self.semaphore),
            mock.patch.object(limits, "PATH", self.root / "usage_limit.json"),
            mock.patch.object(resume, "PATH", self.root / "interrupted_turns.json"),
            mock.patch.object(config, "AGENT_STALL_AFTER", 0.08),
            mock.patch.object(config, "AUTO_FALLBACK", True),
            mock.patch.object(config, "SECONDARY_PROVIDER", "kimi"),
            mock.patch.object(config, "SECONDARY_MODEL", "kimi-test"),
            mock.patch.object(config, "PUBLIC_BASE_URL", None),
            mock.patch.object(config, "STEER_HOLD", 0),
            mock.patch.object(bot, "DISCORD_CALL_TIMEOUT", 0.05),
            mock.patch.object(bot, "DISCORD_REPLY_TIMEOUT", 0.05),
            mock.patch.object(bot, "DISCORD_REPLY_RETRIES", 1),
            mock.patch.object(bot, "DISCORD_REPLY_RETRY_DELAY", 0.001),
            mock.patch.object(bot, "worklog", None),
            mock.patch.object(bot, "wa", None),
            mock.patch.object(bot, "_harness_brief", return_value="[test brief]\n"),
            mock.patch.object(bot, "_listening_ports", return_value=set()),
            mock.patch.object(bot, "_start_limit_resumer"),
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
        ):
            patch = mock.patch.dict(target, {}, clear=True)
            patch.start()
            self.addCleanup(patch.stop)

    def kill_spawned(self):
        for pid in self.spawned_pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    def instance(self, provider="claude", channel_id=17, **kwargs):
        inst = Instance(
            channel_id=channel_id,
            name=kwargs.pop("name", f"stall-agent-{channel_id}"),
            provider=provider,
            workdir=str(self.root),
            **kwargs,
        )
        bot.store.put(inst)
        return inst

    async def recover(self, inst, providers, prompt="build the feature"):
        channel = StatusChannel()
        status = FakeStatus(channel)
        with mock.patch.object(bot, "get_provider", side_effect=providers.__getitem__):
            await asyncio.wait_for(bot._run_turn_recovering(inst, prompt, status), 3)
        self.assertNotIn(inst.channel_id, bot._active_turns)
        return channel, status

    async def run_direct(self, inst, provider, prompt="build the feature"):
        status = FakeStatus(StatusChannel())
        with mock.patch.object(bot, "get_provider", return_value=provider):
            result = await asyncio.wait_for(bot._run_turn(inst, prompt, status), 3)
        self.assertNotIn(inst.channel_id, bot._active_turns)
        return result, status

    async def test_crash_retry_stall_gets_continuation_then_finishes(self):
        inst = self.instance(session_id="original")
        gate = asyncio.Event()
        provider = TurnScriptProvider(
            [StreamEvent("error", text="crash")],
            [StreamEvent("session", session_id="saved"), gate.wait()],
            done("recovered", "saved"),
        )

        await self.recover(inst, {"claude": provider})

        self.assertEqual(len(provider.calls), 3)
        self.assertEqual(provider.calls[2]["session_id"], "saved")
        self.assertIn("[provider stall]", provider.calls[2]["prompt"])

    async def test_crash_retry_limit_still_uses_fallback(self):
        inst = self.instance()
        claude = TurnScriptProvider(
            [StreamEvent("error", text="crash")],
            limited(time.time() + 3600),
        )
        kimi = ScriptedProvider(done("secondary reply", "kimi-session"))

        await self.recover(inst, {"claude": claude, "kimi": kimi})

        self.assertEqual(len(claude.calls), 2)
        self.assertEqual(len(kimi.calls), 1)
        self.assertEqual(inst.secondary_session_id, "kimi-session")

    async def test_two_silent_turns_stop_after_exactly_one_retry_with_help(self):
        inst = self.instance(session_id="claude-session")
        gate = asyncio.Event()
        provider = TurnScriptProvider([gate.wait()], [gate.wait()])

        channel, _ = await self.recover(inst, {"claude": provider})

        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(provider.closed, 2)
        self.assertEqual(provider.calls[1]["session_id"], "claude-session")
        self.assertIn("[provider stall]", provider.calls[1]["prompt"])
        self.assertIn("build the feature", provider.calls[1]["prompt"])
        self.assertEqual(len(channel.statuses), 1, "only one continuation status is created")
        help_text = channel.statuses[0].edits[-1]["content"]
        self.assertIn("stalled twice without producing events", help_text)
        self.assertIn("No more automatic retries", help_text)
        self.assertIn("Session `claude-session` is preserved", help_text)
        self.assertIn("Check the provider login and CLI processes", help_text)
        self.assertIn("AGENT_STALL_AFTER", help_text)

    async def test_first_stall_then_done_continues_once_on_captured_session(self):
        inst = self.instance(session_id="claude-old")
        gate = asyncio.Event()
        provider = TurnScriptProvider(
            [StreamEvent("session", session_id="claude-captured"), gate.wait()],
            done("continued reply", "claude-captured"),
        )

        channel, _ = await self.recover(inst, {"claude": provider})

        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(provider.calls[0]["session_id"], "claude-old")
        self.assertEqual(provider.calls[1]["session_id"], "claude-captured")
        self.assertEqual(inst.session_id, "claude-captured")
        self.assertIn("[provider stall]", provider.calls[1]["prompt"])
        self.assertEqual(channel.statuses[0].edits[-1]["content"], "continued reply")

    async def test_activity_events_each_reset_the_deadline(self):
        inst = self.instance()
        provider = TurnScriptProvider([
            asyncio.sleep(0.03),
            StreamEvent("session", session_id="active-session"),
            asyncio.sleep(0.03),
            StreamEvent("status", text="ran a tool"),
            asyncio.sleep(0.03),
            StreamEvent("text", text="partial"),
            asyncio.sleep(0.03),
            *done("finished after activity", "active-session"),
        ])

        started = time.monotonic()
        result, status = await self.run_direct(inst, provider)
        elapsed = time.monotonic() - started

        self.assertGreater(elapsed, config.AGENT_STALL_AFTER)
        self.assertEqual(result, "done")
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(status.edits[-1]["content"], "finished after activity")

    async def test_user_interruption_does_not_trigger_a_stall_retry(self):
        inst = self.instance()
        gate = asyncio.Event()
        provider = TurnScriptProvider([gate.wait()])
        channel = StatusChannel()
        status = FakeStatus(channel)
        with mock.patch.object(bot, "get_provider", return_value=provider):
            task = asyncio.create_task(bot._run_turn_recovering(inst, "stop when asked", status))
            await asyncio.wait_for(provider.started.wait(), 1)
            turn = bot._active_turns[inst.channel_id]
            turn["interrupted_by"] = "tester"
            turn["interrupt"].set()
            await asyncio.wait_for(task, 1)

        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(provider.closed, 1)
        self.assertEqual(channel.sent, [])
        self.assertIn("Interrupted by tester", status.edits[-1]["content"])
        self.assertNotIn(inst.channel_id, bot._active_turns)

    async def test_unread_live_input_is_preserved_exactly_once(self):
        inst = self.instance()
        gate = asyncio.Event()
        unread_message = mock.MagicMock(id=987)
        provider = None

        def inject_unread(call, extra):
            if len(provider.calls) != 1:
                return
            message_id = extra["control"].send("unread correction")
            bot._active_turns[inst.channel_id]["steered"][message_id] = unread_message

        provider = TurnScriptProvider(
            [gate.wait()],
            done("continued", "claude-session"),
            on_turn=inject_unread,
        )
        provider.supports_graceful_interrupt = True
        provider.supports_live_input = True
        queued = mock.AsyncMock()

        with mock.patch.object(bot, "_process_user_message", new=queued):
            await self.recover(inst, {"claude": provider})
            await asyncio.sleep(0)

        in_continuation = any(
            "unread correction" in call["prompt"] for call in provider.calls[1:]
        )
        self.assertEqual(int(in_continuation) + queued.await_count, 1)
        if not in_continuation:
            queued.assert_awaited_once_with(inst, unread_message)

    async def test_secondary_stalls_record_handoff_and_preserve_primary(self):
        inst = self.instance(session_id="claude-original")
        claude = ScriptedProvider(limited(time.time() + 3600, "claude-captured"))
        gate = asyncio.Event()
        kimi = TurnScriptProvider(
            [StreamEvent("session", session_id="kimi-captured"), gate.wait()],
            [gate.wait()],
        )

        channel, _ = await self.recover(inst, {"claude": claude, "kimi": kimi})

        self.assertEqual(len(claude.calls), 1)
        self.assertEqual(len(kimi.calls), 2)
        self.assertIsNone(kimi.calls[0]["session_id"])
        self.assertEqual(kimi.calls[1]["session_id"], "kimi-captured")
        self.assertEqual(inst.session_id, "claude-captured")
        self.assertEqual(inst.secondary_session_id, "kimi-captured")
        self.assertEqual(inst.fallback_handoff.count("Secondary outcome: stalled"), 2)
        self.assertIn("Secondary request:", inst.fallback_handoff)
        self.assertIn("Secondary reply:\n(no reply)", inst.fallback_handoff)
        help_text = channel.statuses[-1].edits[-1]["content"]
        self.assertIn("`kimi` stalled twice", help_text)
        self.assertIn("Session `kimi-captured` is preserved", help_text)

    async def test_background_only_wait_disables_the_stall_deadline(self):
        inst = self.instance()
        provider = TurnScriptProvider([
            StreamEvent("background", text="1"),
            asyncio.sleep(0.15),
            StreamEvent("background", text="0"),
            *done("background finished", "background-session"),
        ])

        result, status = await self.run_direct(inst, provider)

        self.assertEqual(result, "done")
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(status.edits[-1]["content"], "background finished")
        self.assertEqual(self.semaphore._value, 1)

    async def test_active_state_is_cleared_for_every_turn_exit(self):
        gate = asyncio.Event()
        cases = [
            ("done", done("reply", "done-session"), "done"),
            ("limited", limited(time.time() + 3600), "limited"),
            ("interrupted", [StreamEvent("interrupted", text="partial")], "interrupted"),
            ("crashed", [StreamEvent("error", text="cli failed")], "crashed"),
            ("error", [StreamEvent("status", text="tool"), StreamEvent("error", text="failed")], "error"),
            ("stalled", [gate.wait()], "stalled"),
            ("cancelled", [gate.wait()], None),
        ]
        for offset, (name, script, expected) in enumerate(cases, 100):
            with self.subTest(exit=name):
                inst = self.instance(channel_id=offset)
                provider = TurnScriptProvider(script)
                if name == "cancelled":
                    status = FakeStatus(StatusChannel())
                    with mock.patch.object(bot, "get_provider", return_value=provider):
                        task = asyncio.create_task(bot._run_turn(inst, "cancel this", status))
                        await asyncio.wait_for(provider.started.wait(), 1)
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                else:
                    result, _ = await self.run_direct(inst, provider)
                    self.assertEqual(result, expected)
                self.assertNotIn(inst.channel_id, bot._active_turns)
        self.assertEqual(bot._active_turns, {})

    @unittest.skipIf(os.name == "nt", "POSIX process-tree inspection")
    async def test_real_claude_kimi_and_muse_clis_are_killed_by_central_stall(self):
        for provider_name in ("claude", "kimi", "muse"):
            with self.subTest(provider=provider_name):
                pid_file = self.root / f"{provider_name}.pids"
                fake = self.root / provider_name
                fake.write_text("#!" + sys.executable + "\n" + textwrap.dedent(f"""
                    import os, subprocess, sys, time
                    from pathlib import Path
                    sleeper = [sys.executable, "-c", "import time; time.sleep(60)"]
                    child = subprocess.Popen(sleeper)
                    tool = subprocess.Popen(sleeper, preexec_fn=os.setpgrp)
                    Path({str(pid_file)!r}).write_text(f"{{os.getpid()}} {{child.pid}} {{tool.pid}}")
                    time.sleep(60)
                """))
                fake.chmod(0o755)
                inst = self.instance(provider=provider_name, channel_id=200 + len(self.spawned_pids))
                status = FakeStatus(StatusChannel())
                with mock.patch.object(config, f"{provider_name.upper()}_BIN", str(fake)):
                    result = await asyncio.wait_for(
                        bot._run_turn(inst, "hang without events", status), 3
                    )
                self.assertNotIn(inst.channel_id, bot._active_turns)

                self.assertEqual(result, "stalled")
                pids = [int(pid) for pid in pid_file.read_text().split()]
                self.assertEqual(len(pids), 3)
                self.spawned_pids.update(pids)
                deadline = time.monotonic() + 3
                while any(alive(pid) for pid in pids) and time.monotonic() < deadline:
                    await asyncio.sleep(0.02)
                self.assertFalse(
                    [pid for pid in pids if alive(pid)],
                    f"{provider_name} CLI, child, and separately-grouped tool should all be dead",
                )


if __name__ == "__main__":
    unittest.main()

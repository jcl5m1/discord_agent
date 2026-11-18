"""Turns cut off by a restart: the record, the resume prompt, and the start-up resume.

    .venv/bin/python -m unittest tests.test_resume
"""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import resume


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(resume, "PATH", Path(self.dir.name) / "interrupted_turns.json")
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.dir.cleanup()

    def test_mark_clear_take(self):
        resume.mark(1, "a", "do x", 11)
        resume.mark(2, "b", "do y")
        resume.clear(1)
        records = resume.take()
        self.assertEqual(list(records), [2])
        self.assertEqual(records[2]["instance"], "b")
        self.assertIsNone(records[2]["message_id"])
        self.assertEqual(resume.take(), {})  # taken once

    def test_long_prompt_is_cut(self):
        resume.mark(1, "a", "x" * 5000)
        self.assertEqual(len(resume.take()[1]["prompt"]), resume.PREVIEW)

    def test_prompt_says_what_to_do(self):
        text = resume.prompt({"started": 0, "prompt": "build the maps", "resumes": 1})
        self.assertIn("[harness restart]", text)
        self.assertIn("build the maps", text)
        self.assertIn("Background subagents", text)
        self.assertIn("an earlier resume", text)


class StartupTests(unittest.TestCase):
    def setUp(self):
        from harness import bot
        self.bot = bot
        self.dir = tempfile.TemporaryDirectory()
        self.patches = [mock.patch.object(resume, "PATH", Path(self.dir.name) / "interrupted_turns.json")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.dir.cleanup()

    def _run(self, inst, channel, auto=True):
        bot = self.bot
        started = []

        async def fake_resume(i, ch, record, count):
            started.append((i.name, count, record["prompt"]))

        async def go():
            with mock.patch.object(bot.store, "get", return_value=inst), \
                 mock.patch.object(bot.store, "put"), \
                 mock.patch.object(bot.client, "get_channel", return_value=channel), \
                 mock.patch.object(bot, "_run_resume", fake_resume), \
                 mock.patch.object(bot.config, "AUTO_RESUME", auto):
                bot._resume_cut_off_turns()
                await asyncio.sleep(0)
        asyncio.run(go())
        return started

    def _channel(self):
        import discord
        ch = mock.MagicMock(spec=discord.TextChannel)
        ch.send = mock.AsyncMock()
        return ch

    def test_resumes_and_skips_the_catch_up_of_the_cut_off_message(self):
        inst = mock.MagicMock(stopped=False, last_message_id="100")
        inst.name = "agent"
        resume.mark(5, "agent", "do x", 150)
        started = self._run(inst, self._channel())
        self.assertEqual(started, [("agent", 1, "do x")])
        self.assertEqual(inst.last_message_id, "150")

    def test_stops_after_max_resumes(self):
        inst = mock.MagicMock(stopped=False, last_message_id=None)
        inst.name = "agent"
        ch = self._channel()
        resume.mark(5, "agent", "do x", None, resumes=resume.MAX_RESUMES)
        self.assertEqual(self._run(inst, ch), [])
        ch.send.assert_called_once()

    def test_off_or_stopped_means_no_resume(self):
        inst = mock.MagicMock(stopped=False, last_message_id=None)
        inst.name = "agent"
        resume.mark(5, "agent", "do x")
        self.assertEqual(self._run(inst, self._channel(), auto=False), [])
        inst.stopped = True
        resume.mark(5, "agent", "do x")
        self.assertEqual(self._run(inst, self._channel()), [])


if __name__ == "__main__":
    unittest.main()

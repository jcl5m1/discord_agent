"""Claude usage limits: detecting one, waiting for the reset, the resume records.

    .venv/bin/python -m unittest tests.test_limits
"""

import asyncio
import json
import sys
import tempfile
import textwrap
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from harness import config, limits
from harness.providers.claude import ClaudeProvider

LIMITED_CLAUDE = r'''
import json, sys
def out(ev): print(json.dumps(ev), flush=True)
sys.stdin.readline()
out({"type": "system", "subtype": "init", "session_id": "sess-1"})
out({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": 1800000000,
     "rateLimitType": "five_hour", "unifiedWindows": {"five_hour": {"status": "rejected", "resetsAt": 1800000000},
     "seven_day": {"status": "allowed", "resetsAt": 1800500000}}}, "session_id": "sess-1"})
out({"type": "result", "is_error": True, "result": "You've hit your limit · resets 9pm", "session_id": "sess-1"})
sys.stdin.read()
'''


class ParseTests(unittest.TestCase):
    def test_reset_from_text(self):
        self.assertEqual(limits.reset_from_text("Claude AI usage limit reached|1800000000"), 1800000000.0)
        now = datetime(2026, 11, 18, 20, 0).timestamp()
        at = datetime.fromtimestamp(limits.reset_from_text("You've hit your limit · resets 9pm", now))
        self.assertEqual((at.day, at.hour, at.minute), (18, 21, 0))
        at = datetime.fromtimestamp(limits.reset_from_text("resets 7:30am (America/Los_Angeles)", now))
        self.assertEqual((at.day, at.hour, at.minute), (19, 7, 30))  # tomorrow
        self.assertIsNone(limits.reset_from_text("all good"))
        self.assertTrue(limits.is_limit_text("You've hit your limit · resets 9pm"))
        self.assertFalse(limits.is_limit_text("the limit of the loop"))


class RecordTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = mock.patch.object(limits, "PATH", Path(tmp.name) / "usage_limit.json")
        p.start()
        self.addCleanup(p.stop)

    def test_hit_wait_and_take(self):
        now = time.time()
        at = limits.hit(now + 600, 1, "a", "build the map", now=now)
        self.assertEqual(at, now + 600 + limits.RESUME_AFTER)
        limits.hit(None, 2, "b", "other", now=now)  # unknown reset: never earlier than the known one
        self.assertEqual(limits.resume_at(now), at)
        self.assertTrue(limits.pending())
        self.assertEqual(list(limits.take(2)), [2])
        self.assertIn("build the map", limits.prompt(limits.take()[1]))
        self.assertIsNone(limits.resume_at(now))
        self.assertFalse(limits.pending())

    def test_resume_time_passed(self):
        limits.hit(time.time() - 120, 1, "a", "x")
        self.assertIsNone(limits.resume_at())
        self.assertTrue(limits.pending())  # the resumer still has to run it


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejected_rate_limit_becomes_a_limit_event(self):
        with tempfile.TemporaryDirectory() as d:
            fake = Path(d) / "claude"
            fake.write_text("#!" + sys.executable + "\n" + textwrap.dedent(LIMITED_CLAUDE))
            fake.chmod(0o755)
            with mock.patch.object(config, "CLAUDE_BIN", str(fake)), mock.patch.object(config, "STEER_HOLD", 0):
                events = [e async for e in ClaudeProvider().run_turn(d, "go", None, "haiku")]
        last = events[-1]
        self.assertEqual((last.kind, last.reset_at, last.session_id), ("limit", 1800000000.0, "sess-1"))
        self.assertIn("hit your limit", last.text)


if __name__ == "__main__":
    unittest.main()

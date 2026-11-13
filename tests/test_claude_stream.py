"""Claude provider's stream-json turn: live messages, graceful interrupt, closing.

Runs against a fake `claude` that speaks the same stdin/stdout protocol.

    .venv/bin/python -m unittest tests.test_claude_stream
"""

import asyncio
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from harness import config
from harness.providers.base import TurnControl
from harness.providers.claude import ClaudeProvider

FAKE_CLAUDE = r'''
import json, select, sys
def out(ev): print(json.dumps(ev), flush=True)
def read(timeout):
    r, _, _ = select.select([sys.stdin], [], [], timeout)
    return sys.stdin.readline() if r else None
first = json.loads(sys.stdin.readline())
out({"type": "system", "subtype": "init", "session_id": "sess-1"})
out({"type": "user", "isReplay": True, "uuid": first["uuid"], "message": first["message"], "session_id": "sess-1"})
out({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {}}]}, "session_id": "sess-1"})
mode = first["message"]["content"]
while True:
    line = read(0.5)
    if line is None:  # tool finished, nothing more to read: end the turn
        out({"type": "assistant", "message": {"content": [{"type": "text", "text": "turn done"}]}})
        out({"type": "result", "subtype": "success", "is_error": False, "result": "turn done"})
        break
    if not line:
        sys.exit(0)
    ev = json.loads(line)
    if ev["type"] == "control_request":
        out({"type": "control_response", "response": {"subtype": "success", "request_id": ev["request_id"]}})
        out({"type": "result", "subtype": "error_during_execution", "is_error": True})
        break
    if ev["type"] == "user":
        out({"type": "user", "isReplay": True, "uuid": ev["uuid"], "message": ev["message"]})
        out({"type": "assistant", "message": {"content": [{"type": "text", "text": "read: " + ev["message"]["content"]}]}})
# after the result, answer any message sent near the end in a further turn
while True:
    line = sys.stdin.readline()
    if not line:
        sys.exit(0)
    ev = json.loads(line)
    if ev["type"] == "user":
        out({"type": "user", "isReplay": True, "uuid": ev["uuid"], "message": ev["message"]})
        out({"type": "assistant", "message": {"content": [{"type": "text", "text": "late: " + ev["message"]["content"]}]}})
        out({"type": "result", "subtype": "success", "is_error": False})
'''


class ClaudeStreamTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        fake = Path(self.tmp.name) / "claude"
        fake.write_text("#!" + sys.executable + "\n" + textwrap.dedent(FAKE_CLAUDE))
        fake.chmod(0o755)
        patcher = mock.patch.object(config, "CLAUDE_BIN", str(fake))
        patcher.start()
        self.addCleanup(patcher.stop)

    async def run_turn(self, control=None, during=None):
        events = []
        async for ev in ClaudeProvider().run_turn(self.tmp.name, "go", None, "haiku", control=control):
            events.append(ev)
            if ev.kind == "status" and during:
                during()
        return events

    def kinds(self, events):
        return [e.kind for e in events]

    async def test_plain_turn_ends_on_result(self):
        events = await asyncio.wait_for(self.run_turn(), 10)
        self.assertEqual(self.kinds(events), ["session", "status", "text", "done"])
        self.assertEqual(events[-1].session_id, "sess-1")

    async def test_message_sent_mid_turn_is_read_in_the_same_turn(self):
        control = TurnControl()
        sent = []
        events = await asyncio.wait_for(
            self.run_turn(control, during=lambda: sent.append(control.send("also this"))), 10
        )
        self.assertIn("delivered", self.kinds(events))
        self.assertEqual(next(e.text for e in events if e.kind == "delivered"), sent[0])
        self.assertIn("read: also this", events[-1].text)
        self.assertEqual(events[-1].kind, "done")

    async def test_graceful_interrupt(self):
        control = TurnControl()
        events = await asyncio.wait_for(self.run_turn(control, during=control.interrupt), 10)
        self.assertEqual(events[-1].kind, "interrupted")
        self.assertEqual(events[-1].session_id, "sess-1")

    async def test_message_unread_at_the_end_gets_a_further_turn(self):
        control = TurnControl()
        events = []
        async for ev in ClaudeProvider().run_turn(self.tmp.name, "go", None, "haiku", control=control):
            events.append(ev)
            if ev.kind == "text" and ev.text == "turn done":
                control.send("one more")  # arrives as the turn ends
        self.assertIn("late: one more", events[-1].text)
        self.assertEqual(events[-1].kind, "done")


if __name__ == "__main__":
    unittest.main()

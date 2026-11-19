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
    line = read(2.5)
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
        for attr, value in (("CLAUDE_BIN", str(fake)), ("STEER_HOLD", 0)):
            patcher = mock.patch.object(config, attr, value)
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


class HoldTests(unittest.TestCase):
    """TurnControl keeps sent messages editable until they're handed over."""

    def test_edit_and_withdraw_while_held(self):
        c = TurnControl()
        kept, dropped = c.send("helo"), c.send("never mind")
        self.assertTrue(c.edit(kept, "hello"))
        self.assertTrue(c.withdraw(dropped))
        self.assertEqual(c.take_held(), [(kept, "hello")])
        self.assertEqual(c.unread, {kept})

    def test_too_late_once_handed_over(self):
        c = TurnControl()
        m = c.send("x")
        c.take_held()
        self.assertFalse(c.edit(m, "y"))
        self.assertFalse(c.withdraw(m))

    def test_only_messages_past_their_window_are_due(self):
        import time

        c = TurnControl()
        old = c.send("old")
        c.held[old][1] -= 30
        new = c.send("new")
        self.assertEqual(c.take_held(time.monotonic() - 10), [(old, "old")])
        self.assertEqual([m for m, _ in c.take_held()], [new])


if __name__ == "__main__":
    unittest.main()


# A fake CLI with one background task, as the real CLI reports it. If its input closes while the task runs, it
# gives up on the task after a short time (the real CLI after about 10 minutes) and exits: the task dies.
FAKE_BACKGROUND = r'''
import json, select, sys, time
def out(ev): print(json.dumps(ev), flush=True)
first = json.loads(sys.stdin.readline())
out({"type": "system", "subtype": "init", "session_id": "sess-bg"})
out({"type": "system", "subtype": "background_tasks_changed", "tasks": [{"task_id": "t1"}]})
out({"type": "assistant", "message": {"content": [{"type": "text", "text": "started"}]}})
out({"type": "result", "subtype": "success", "is_error": False})
deadline = time.time() + 1.5
while time.time() < deadline:
    r, _, _ = select.select([sys.stdin], [], [], 0.1)
    if r and not sys.stdin.readline():
        time.sleep(0.5)
        out({"type": "assistant", "message": {"content": [{"type": "text", "text": " | task killed"}]}})
        sys.exit(0)
out({"type": "system", "subtype": "background_tasks_changed", "tasks": []})
out({"type": "assistant", "message": {"content": [{"type": "text", "text": " | task finished"}]}})
out({"type": "result", "subtype": "success", "is_error": False})
sys.stdin.readline()
'''


class ClaudeBackgroundTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        fake = Path(self.tmp.name) / "claude"
        fake.write_text("#!" + sys.executable + "\n" + textwrap.dedent(FAKE_BACKGROUND))
        fake.chmod(0o755)
        patcher = mock.patch.object(config, "CLAUDE_BIN", str(fake))
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_input_stays_open_until_background_tasks_end(self):
        events = [ev async for ev in ClaudeProvider().run_turn(self.tmp.name, "go", None, "haiku")]
        self.assertEqual([ev.text for ev in events if ev.kind == "background"], ["1", "0"])
        # The CLI reports the task before its reply; the turn hears of it only after the reply (slot accounting).
        first_bg = next(i for i, ev in enumerate(events) if ev.kind == "background")
        reply = next(i for i, ev in enumerate(events) if ev.kind == "text" and "started" in ev.text)
        self.assertGreater(first_bg, reply)
        done = next(ev for ev in events if ev.kind == "done")
        self.assertIn("task finished", done.text)
        self.assertNotIn("task killed", done.text)


# After the reply, one background task runs; a message sent then is read and answered in the same process,
# and the task still finishes.
FAKE_BACKGROUND_STEER = r'''
import json, select, sys, time
def out(ev): print(json.dumps(ev), flush=True)
first = json.loads(sys.stdin.readline())
out({"type": "system", "subtype": "init", "session_id": "sess-bg"})
out({"type": "system", "subtype": "background_tasks_changed", "tasks": [{"task_id": "t1"}]})
out({"type": "assistant", "message": {"content": [{"type": "text", "text": "started"}]}})
out({"type": "result", "subtype": "success", "is_error": False})
deadline = time.time() + 2.0
while time.time() < deadline:
    r, _, _ = select.select([sys.stdin], [], [], 0.1)
    if r:
        line = sys.stdin.readline()
        if not line:
            out({"type": "assistant", "message": {"content": [{"type": "text", "text": " | task killed"}]}})
            sys.exit(0)
        ev = json.loads(line)
        out({"type": "user", "isReplay": True, "uuid": ev["uuid"], "message": ev["message"]})
        out({"type": "assistant", "message": {"content": [{"type": "text", "text": " | late: " + ev["message"]["content"]}]}})
        out({"type": "result", "subtype": "success", "is_error": False})
out({"type": "system", "subtype": "background_tasks_changed", "tasks": []})
out({"type": "assistant", "message": {"content": [{"type": "text", "text": " | task finished"}]}})
out({"type": "result", "subtype": "success", "is_error": False})
sys.stdin.readline()
'''


class ClaudeBackgroundSteerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        fake = Path(self.tmp.name) / "claude"
        fake.write_text("#!" + sys.executable + "\n" + textwrap.dedent(FAKE_BACKGROUND_STEER))
        fake.chmod(0o755)
        for attr, value in (("CLAUDE_BIN", str(fake)), ("STEER_HOLD", 0)):
            patcher = mock.patch.object(config, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_message_after_reply_goes_into_the_held_session(self):
        control = TurnControl()
        events, sent = [], None
        async for ev in ClaudeProvider().run_turn(self.tmp.name, "go", None, "haiku", control=control):
            events.append(ev)
            if ev.kind == "background" and ev.text == "1" and sent is None:
                sent = control.send("hello")  # the user writes after the reply, while the task runs
        kinds = [(ev.kind, ev.text) for ev in events if ev.kind in ("background", "delivered")]
        self.assertEqual(kinds[0], ("background", "1"))  # nothing before the reply
        self.assertIn(("delivered", sent), kinds)
        self.assertEqual(kinds[-1], ("background", "0"))
        done = next(ev for ev in events if ev.kind == "done")
        self.assertIn("late: hello", done.text)
        self.assertIn("task finished", done.text)

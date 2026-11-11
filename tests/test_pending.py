"""Edits and deletes of Discord messages waiting for, or already in, a turn.

    .venv/bin/python -m unittest tests.test_pending
"""

import unittest

from harness.pending import PendingTurns


class PendingTurnsTests(unittest.TestCase):
    def setUp(self):
        self.p = PendingTurns(remember=3)

    def test_edit_while_queued_replaces_text(self):
        turn = self.p.add(1, "helo")
        self.assertEqual(self.p.edit(1, "hello"), ("updated", turn))
        self.assertEqual(turn.prompt(), "hello")

    def test_unchanged_edit_ignored(self):  # e.g. Discord adding a link preview
        self.p.add(1, "see https://x.y")
        self.assertEqual(self.p.edit(1, "see https://x.y"), ("ignored", None))

    def test_unknown_message_ignored(self):
        self.assertEqual(self.p.edit(99, "x"), ("ignored", None))

    def test_edit_while_running_queues_followup(self):
        turn = self.p.add(1, "old")
        turn.started = True
        outcome, follow = self.p.edit(1, "new")
        self.assertEqual(outcome, "followup")
        self.assertEqual(follow.edit_of, "old")
        self.assertIn("old", follow.prompt())
        self.assertIn("new", follow.prompt())
        # another edit while the follow-up waits updates it, not a third turn
        self.assertEqual(self.p.edit(1, "newer"), ("updated", follow))
        self.assertEqual(follow.edit_of, "old")
        self.assertTrue(follow.prompt().endswith("newer"))

    def test_edit_after_finish_queues_followup(self):
        turn = self.p.add(1, "old")
        turn.started = True
        self.p.finish(turn)
        outcome, follow = self.p.edit(1, "new")
        self.assertEqual((outcome, follow.edit_of), ("followup", "old"))

    def test_delete_drops_only_waiting_turns(self):
        running = self.p.add(1, "a")
        running.started = True
        waiting = self.p.add(1, "b", edit_of="a")
        self.assertEqual(self.p.delete(1), 1)
        self.assertTrue(waiting.deleted)
        self.assertFalse(running.deleted)

    def test_deleted_message_forgotten_for_later_edits(self):
        turn = self.p.add(1, "a")
        turn.started = True
        self.p.finish(turn)
        self.p.delete(1)
        self.assertEqual(self.p.edit(1, "b"), ("ignored", None))

    def test_unstarted_turn_not_remembered(self):
        turn = self.p.add(1, "a")
        turn.deleted = True
        self.p.finish(turn)
        self.assertEqual(self.p.edit(1, "b"), ("ignored", None))

    def test_memory_is_bounded(self):
        for i in range(5):
            t = self.p.add(i, "x")
            t.started = True
            self.p.finish(t)
        self.assertEqual(self.p.edit(0, "y"), ("ignored", None))
        self.assertEqual(self.p.edit(4, "y")[0], "followup")


if __name__ == "__main__":
    unittest.main()

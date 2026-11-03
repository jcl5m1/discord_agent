"""Which channels this bot takes slash commands in.

    .venv/bin/python -m unittest tests.test_guard
"""

import unittest
from types import SimpleNamespace

from harness.guard import channel_allowed

OWN_CATEGORY = 50


def ch(id, name="x", category_id=None):
    return SimpleNamespace(id=id, name=name, category_id=category_id)


class GuardTests(unittest.TestCase):
    def allowed(self, channel):
        return channel_allowed(
            channel, category_id=OWN_CATEGORY, shared={"general", "77"}, instance_channel_ids={9}
        )

    def test_own_category_shared_and_instance_channels(self):
        self.assertTrue(self.allowed(ch(1, category_id=OWN_CATEGORY)))
        self.assertTrue(self.allowed(ch(2, name="general", category_id=60)))
        self.assertTrue(self.allowed(ch(77, category_id=60)))
        self.assertTrue(self.allowed(ch(9, category_id=60)))

    def test_other_bots_channels_refused(self):
        self.assertFalse(self.allowed(ch(3, name="livekit", category_id=60)))
        self.assertFalse(self.allowed(ch(4, name="ci-remote", category_id=None)))

    def test_thread_follows_its_parent(self):
        own = ch(1, category_id=OWN_CATEGORY)
        other = ch(3, category_id=60)
        self.assertTrue(self.allowed(SimpleNamespace(id=500, name="t", parent=own)))
        self.assertFalse(self.allowed(SimpleNamespace(id=501, name="general", parent=other)))


if __name__ == "__main__":
    unittest.main()

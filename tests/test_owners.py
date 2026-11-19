"""Owners of processes: container, cron, unit with its limit, heavy job, harness agent.

    .venv/bin/python -m unittest tests.test_owners
"""

import unittest
from unittest import mock

from harness import owners

CID = "a" * 64


class OwnerTests(unittest.TestCase):
    def ctx(self):
        with mock.patch.object(owners.shutil, "which", return_value=None), \
                mock.patch.object(owners, "config") as cfg:
            cfg.DATA_DIR.__truediv__.return_value.read_text.side_effect = OSError
            c = owners.Context()
        c.containers = {CID: "placeframe-localizer-cuda-1"}
        c.sessions = {"f7f3558b-3924-4c0e-a4c6-5c3c6a9a69e8": "placeframe-compute"}
        c.heavy = {"heavy-video-decode-42": "harness-2@m"}
        return c

    def owner(self, cgroup, agent=None, limit="8589934592"):
        with mock.patch.object(owners, "_cgroup", return_value=cgroup), \
                mock.patch.object(owners, "_agent_of", return_value=agent), \
                mock.patch.object(owners.Path, "read_text", return_value=limit):
            return owners.owner(1234, self.ctx())

    def test_kinds(self):
        self.assertEqual(self.owner(f"/system.slice/docker-{CID}.scope"), "container placeframe-localizer-cuda-1")
        self.assertEqual(self.owner("/system.slice/cron.service"), "cron")
        self.assertEqual(self.owner("/user.slice/user-1000.slice/user@1000.service/app.slice/pf-frames-rebuild.service"),
                         "unit pf-frames-rebuild (MemoryMax 8G)")
        self.assertEqual(self.owner("/user.slice/user-1000.slice/user@1000.service/app.slice/pf-x.service", limit="max"),
                         "unit pf-x")
        self.assertEqual(self.owner("/user.slice/.../app.slice/heavy-video-decode-42.scope"),
                         "heavy video-decode job of harness-2@m")
        self.assertEqual(self.owner("/user.slice/.../app.slice/discord-agent.service", agent="placeframe-compute"),
                         "agent placeframe-compute")
        self.assertEqual(self.owner("/user.slice/user-1000.slice/session-12.scope"), "a terminal or SSH session")

    def test_session_regex(self):
        m = owners._SESSION.search("claude --resume f7f3558b-3924-4c0e-a4c6-5c3c6a9a69e8 --model x")
        self.assertEqual(m.group(1), "f7f3558b-3924-4c0e-a4c6-5c3c6a9a69e8")


if __name__ == "__main__":
    unittest.main()

"""Disk space: levels, the watchdog's alerts, and the uptime monitor on a full disk.

    .venv/bin/python -m unittest tests.test_disk
"""

import errno
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import disk

GB = 1024 ** 3


class LevelTests(unittest.TestCase):
    def test_levels_and_hysteresis(self):
        self.assertEqual(disk.level(80, 90 * GB), "ok")
        self.assertEqual(disk.level(91, 40 * GB), "warn")
        self.assertEqual(disk.level(96, 15 * GB), "critical")
        self.assertEqual(disk.level(100, 10 * 1024 * 1024), "full")
        self.assertEqual(disk.level(87, 60 * GB, prev="warn"), "warn")  # not yet under 85%
        self.assertEqual(disk.level(84, 70 * GB, prev="warn"), "ok")
        self.assertTrue(disk.is_full_error(OSError(errno.ENOSPC, "No space left on device")))
        self.assertFalse(disk.is_full_error(OSError(errno.EACCES, "denied")))
        self.assertEqual(disk.fmt_bytes(int(1.5 * GB)), "1.5 GB")


class WatchdogTests(unittest.TestCase):
    def test_one_alert_per_rise_and_one_when_back(self):
        import harness.watchdog as wd

        posts = []
        state = {"pct": 80.0, "free": 90 * GB}
        with mock.patch.object(wd.disk, "paths", return_value=[Path("/")]), \
                mock.patch.object(wd.disk, "usage", side_effect=lambda p: (state["pct"], state["free"])), \
                mock.patch.object(wd.disk, "biggest_dirs", return_value=[(50 * GB, "/home/u/data")]), \
                mock.patch.object(wd, "_post_all", side_effect=lambda ch, text: posts.append(text)):
            levels = {}
            for pct, free in ((80, 90), (91, 40), (92, 35), (96, 15), (91, 40), (100, 0), (60, 180)):
                state.update(pct=float(pct), free=free * GB)
                wd._check_disks(levels, [1])
        kinds = [p.split("**")[1] for p in posts]
        self.assertEqual(kinds, ["disk warn", "disk critical", "disk full", "disk back to normal"])
        self.assertIn("/home/u/data", posts[0])


class UptimeFullDiskTests(unittest.TestCase):
    def test_events_survive_a_full_disk(self):
        from harness import uptime

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "uptime.jsonl"
            real_open = Path.open
            full = {"on": True}

            def fake_open(self, *a, **kw):
                if self == path and full["on"]:
                    raise OSError(errno.ENOSPC, "No space left on device")
                return real_open(self, *a, **kw)

            with mock.patch.object(uptime, "LOG_PATH", path), mock.patch.object(Path, "open", fake_open), \
                    mock.patch.object(uptime, "STATE_PATH", Path(d) / "state.json"):
                uptime._append("net_down", targets=[])  # no exception
                uptime._write_state({"x": 1})
                full["on"] = False
                uptime._append("net_up", down_since=0)
            events = [json.loads(line)["event"] for line in path.read_text().splitlines()]
        self.assertEqual(events, ["net_down", "net_up"])


if __name__ == "__main__":
    unittest.main()

"""System health: memory levels, which process may be stopped, the last-resort action.

    .venv/bin/python -m unittest tests.test_health
"""

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from harness import health


class ClassifyTests(unittest.TestCase):
    def test_levels(self):
        self.assertEqual(health.classify(50, 10, 1, 0)[0], "ok")
        self.assertEqual(health.classify(15, 10, 1, 0)[0], "warn")
        self.assertEqual(health.classify(25, 85, 1, 0)[0], "warn")  # swap nearly full, RAM getting low
        self.assertEqual(health.classify(50, 10, 25, 0)[0], "warn")  # memory pressure
        level, reasons = health.classify(4, 99, 60, 10)
        self.assertEqual(level, "critical")
        self.assertIn("4.0% of RAM", reasons[0])
        self.assertEqual(health.classify(40, 10, 50, 35)[0], "critical")  # tasks fully stalled


@unittest.skipUnless(sys.platform.startswith("linux"), "uses /proc")
class TargetTests(unittest.TestCase):
    def test_protected_and_small_processes_are_skipped(self):
        top = [{"pid": os.getpid(), "rss_mib": 9000, "swap_mib": 0, "cmd": "python me"},
               {"pid": 999999, "rss_mib": 5000, "swap_mib": 0, "cmd": "python -m harness.bot"},
               {"pid": 999998, "rss_mib": 3000, "swap_mib": 100, "cmd": "python big.py"},
               {"pid": 999997, "rss_mib": 2000, "swap_mib": 0, "cmd": "other"}]
        self.assertEqual(health.pick_target(top)["pid"], 999998)
        self.assertIsNone(health.pick_target([{"pid": 999998, "rss_mib": 500, "swap_mib": 0, "cmd": "x"}]))
        self.assertIsNone(health.pick_target(top[:2]))

    def test_container_id(self):
        cid = "a" * 64
        with tempfile.TemporaryDirectory() as d:
            Path(d, "cgroup").write_text(f"0::/system.slice/docker-{cid}.scope\n")
            with mock.patch.object(health, "Path", lambda p: Path(d, "cgroup") if p.endswith("cgroup") else Path(p)):
                self.assertEqual(health.container_of(1), cid)

    def test_stops_our_biggest_process_then_kills_it(self):
        child = subprocess.Popen([sys.executable, "-c",
                                  "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); "
                                  "time.sleep(60)"], stdout=subprocess.PIPE)
        child.stdout.readline()
        self.addCleanup(child.kill)
        events = []
        actor = health.Actor(lambda ev, **kw: events.append((ev, kw)))
        top = [{"pid": child.pid, "rss_mib": 4000, "swap_mib": 0, "cmd": "python leak.py"}]
        with mock.patch.object(health, "KILL_AFTER", 0.2):
            self.assertIn("stop pid", actor.act(top))
            self.assertIsNone(actor.act(top))  # waits for the stop request to work
            time.sleep(0.3)
            self.assertIn("kill pid", actor.act(top))  # it ignored SIGTERM
        self.assertEqual(child.wait(5), -9)
        self.assertEqual([kw["action"] for _, kw in events], ["stop", "kill"])


if __name__ == "__main__":
    unittest.main()

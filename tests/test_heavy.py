"""heavy: machine-wide slots, the health gate, the memory limit, stopping with the caller.

    .venv/bin/python -m unittest tests.test_heavy
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from harness import config, heavy

ROOT = Path(__file__).resolve().parent.parent
HAS_SYSTEMD = shutil.which("systemd-run") and subprocess.run(
    ["systemctl", "--user", "show-environment"], capture_output=True).returncode == 0


class SlotAndGateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = Path(tmp.name)
        for p in (mock.patch.object(heavy, "DIR", self.data / "heavy"),
                  mock.patch.object(config, "DATA_DIR", self.data),
                  mock.patch.object(config, "HEAVY_SLOTS", "video-decode=1,default=2")):
            p.start()
            self.addCleanup(p.stop)

    def test_one_slot_per_class_and_holders(self):
        a = heavy.take_slot("video-decode", {"who": "a", "cmd": "ffmpeg 1"})
        self.assertIsNotNone(a)
        self.assertIsNone(heavy.take_slot("video-decode", {"who": "b"}))
        self.assertEqual([h["who"] for h in heavy.holders("video-decode")], ["a"])
        self.assertIsNotNone(heavy.take_slot("default", {"who": "c"}))  # other classes are free
        a.release()
        self.assertEqual(heavy.holders("video-decode"), [])
        self.assertIsNotNone(heavy.take_slot("video-decode", {"who": "b"}))

    def test_gate_closes_on_memory_warn_and_full_disk(self):
        self.assertIsNone(heavy.gate())
        (self.data / "health.json").write_text(json.dumps({"ts": time.time(), "level": "warn",
                                                           "reasons": ["15% of RAM available"]}))
        self.assertIn("memory warn", heavy.gate())
        (self.data / "health.json").write_text(json.dumps({"ts": time.time() - 3600, "level": "warn"}))
        self.assertIsNone(heavy.gate())  # stale state: ignored
        (self.data / "disk.json").write_text(json.dumps({"ts": time.time(), "level": "full", "pct": 99.9}))
        self.assertIn("disk full", heavy.gate())

    def test_waits_then_gives_up(self):
        held = heavy.take_slot("video-decode", {"who": "other", "cmd": "ffmpeg"})
        self.addCleanup(held.release)
        with mock.patch.object(heavy, "POLL", 0.05), mock.patch.object(heavy, "sweep", return_value=[]):
            rc = heavy.run("video-decode", ["true"], "64M", "0", None, max_wait=0.2, force=False)
        self.assertEqual(rc, 75)


@unittest.skipUnless(HAS_SYSTEMD, "needs a systemd user session")
class RunTests(unittest.TestCase):
    def heavy(self, *args, **kw):
        return subprocess.Popen([sys.executable, str(ROOT / "bin" / "heavy"), "--class", "test-heavy", *args],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kw)

    def test_memory_limit_stops_the_job_not_the_machine(self):
        p = self.heavy("--mem", "100M", "--swap", "0", "--", sys.executable, "-c",
                       "x = bytearray(400 * 1024 * 1024); print('survived')")
        out, _ = p.communicate(timeout=60)
        self.assertNotEqual(p.returncode, 0)
        self.assertNotIn("survived", out)

    def test_job_stops_with_its_caller(self):
        p = self.heavy("--mem", "100M", "--", "sh", "-c", "sleep 300 & sleep 300; wait")
        unit = None
        for _ in range(50):
            time.sleep(0.1)
            units = subprocess.run(["systemctl", "--user", "list-units", "--type=scope", "--no-legend",
                                    "--plain", f"heavy-test-heavy-{p.pid}*"], capture_output=True, text=True).stdout
            if units.strip():
                unit = units.split()[0]
                break
        self.assertIsNotNone(unit, "the job's scope did not start")
        p.send_signal(signal.SIGTERM)
        p.wait(timeout=60)
        state = subprocess.run(["systemctl", "--user", "is-active", unit], capture_output=True, text=True).stdout
        self.assertNotEqual(state.strip(), "active")


if __name__ == "__main__":
    unittest.main()

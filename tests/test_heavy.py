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


class InputTests(unittest.TestCase):
    def test_inputs_from_flag_and_arguments(self):
        with tempfile.TemporaryDirectory() as d:
            video = Path(d) / "a.insv"
            video.write_bytes(b"x" * 3000)
            other = Path(d) / "b.insv"
            other.write_bytes(b"x" * 3000)
            folder = Path(d) / "imgs"
            folder.mkdir()
            for i in range(3):
                (folder / f"{i}.jpg").write_bytes(b"j" * 10)
            ins = heavy.inputs(["ffmpeg", "-i", str(video), "-vf", "fps=1", f"--in={other}", "/no/such/file",
                                str(Path(d) / "out.mp4")], [str(folder)])
        self.assertEqual([i["path"] for i in ins], [str(folder), str(video), str(other)])
        self.assertEqual((ins[0]["files"], ins[0]["bytes"]), (3, 30))
        self.assertEqual(ins[1]["bytes"], 3000)
        self.assertEqual(ins[1]["id"], ins[2]["id"])  # same size and first MiB: same content id
        self.assertEqual(len(ins[1]["id"]), 16)

    def test_cgroup_usage_of_a_missing_cgroup_is_empty(self):
        self.assertEqual(heavy._cg_usage(Path("/sys/fs/cgroup/no-such-group")), {})


class IoTests(unittest.TestCase):
    def test_disk_under_an_lvm_volume_is_not_counted_twice(self):
        with tempfile.TemporaryDirectory() as d:
            cg = Path(d)
            (cg / "io.stat").write_text("252:0 rbytes=1000 wbytes=200 rios=1\n259:5 rbytes=1000 wbytes=200 rios=1\n"
                                        "8:16 rbytes=50 wbytes=0\n")
            with mock.patch.object(heavy, "_under_dm", return_value={"259:5"}):
                use = heavy._cg_usage(cg)
        self.assertEqual((use["read"], use["written"]), (1050, 200))


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

    def test_log_has_inputs_and_totals(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "in.bin"
            f.write_bytes(b"y" * 5000)
            log = Path(d) / "jobs.jsonl"
            with mock.patch.object(heavy, "LOG", log), mock.patch.object(heavy, "DIR", Path(d)), \
                    mock.patch.object(heavy, "POLL", 0.2), mock.patch.object(heavy, "gate", return_value=None):
                rc = heavy.run("test-heavy", [sys.executable, "-c", "import time; time.sleep(1)", str(f)], "100M",
                               "0", None, max_wait=10, force=True, name="t")
            rec = json.loads(log.read_text().splitlines()[-1])
        self.assertEqual(rc, 0)
        self.assertEqual(rec["inputs"][0]["path"], str(f))
        self.assertEqual(rec["inputs"][0]["bytes"], 5000)
        for k in ("wall_s", "cpu_s", "peak_mib", "read_mib", "written_mib"):  # the old keys stay
            self.assertIn(k, rec)

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

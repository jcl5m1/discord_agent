"""Disk care: cold-data scan, move to the secondary drive and back, old temp files, the levels.

    .venv/bin/python -m unittest tests.test_diskcare
"""

import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from harness import config, diskcare

OLD = time.time() - 30 * 86400


def make(path: Path, size: int = 2048, old: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    if old:
        os.utime(path, (OLD, OLD))


@unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("rsync"), "needs Linux and rsync")
class DiskCareTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home, self.drive = Path(tmp.name) / "home", Path(tmp.name) / "drive"
        self.drive.mkdir()
        for p in (mock.patch.object(diskcare, "HOME", self.home), mock.patch.object(diskcare, "OFFLOAD_MIN_GB", 1e-6),
                  mock.patch.object(diskcare, "ACTIONS_PATH", Path(tmp.name) / "actions.jsonl"),
                  mock.patch.object(diskcare, "_open_paths", return_value=set()),
                  mock.patch.object(diskcare, "USE_CTIME", False)):  # a test can't set ctime back
            p.start()
            self.addCleanup(p.stop)

    def test_scan_finds_cold_folders_only(self):
        make(self.home / "old_data" / "a.bin")
        make(self.home / "mixed" / "cold" / "b.bin")
        make(self.home / "mixed" / "hot" / "c.bin", old=False)
        make(self.home / "repo" / ".git" / "HEAD")
        make(self.home / "repo" / "big.bin")
        make(self.home / ".config" / "x" / "y.bin")
        (self.home / "venv").mkdir()
        (self.home / "venv" / "pyvenv.cfg").write_text("x")
        make(self.home / "venv" / "lib" / "z.bin")
        for d in ("old_data", "mixed/cold", "venv", "venv/lib"):  # folders themselves are old too
            os.utime(self.home / d, (OLD, OLD))
        found = sorted(Path(c["path"]).relative_to(self.home).as_posix() for c in diskcare.scan())
        self.assertEqual(found, ["mixed/cold", "old_data"])

    def test_offload_and_restore(self):
        make(self.home / "old_data" / "sub" / "a.bin", size=5000)
        line = diskcare.offload(str(self.home / "old_data"), str(self.drive))
        self.assertIn("symlink left", line)
        self.assertTrue((self.home / "old_data").is_symlink())
        self.assertEqual((self.home / "old_data" / "sub" / "a.bin").stat().st_size, 5000)  # path still works
        self.assertTrue((self.drive / "old_data" / "sub" / "a.bin").exists())
        diskcare.restore(str(self.home / "old_data"))
        self.assertFalse((self.home / "old_data").is_symlink())
        self.assertTrue((self.home / "old_data" / "sub" / "a.bin").exists())
        self.assertFalse((self.drive / "old_data").exists())
        with self.assertRaises(SystemExit):
            diskcare.offload("/etc", str(self.drive))  # outside the home folder

    def test_old_temp_files_only(self):
        tmp = self.home / "tmp"
        make(tmp / "old.txt")
        os.utime(tmp / "old.txt", (OLD, OLD))
        make(tmp / "new.txt", old=False)
        freed = diskcare._old_tmp(tmp, 2)
        self.assertEqual((freed, sorted(p.name for p in tmp.iterdir())), (2048, ["new.txt"]))

    def test_step_cleans_then_moves_until_target(self):
        G = 1024 ** 3
        usage = iter([(91.0, 10 * G), (91.0, 10 * G), (85.0, 20 * G), (79.0, 30 * G), (79.0, 30 * G)])
        with mock.patch.object(diskcare.disk, "usage", side_effect=lambda p: next(usage)), \
                mock.patch.object(diskcare, "clean", return_value=["cache: 1 GB"]), \
                mock.patch.object(diskcare, "scan", return_value=[{"path": "/a", "gb": 5, "days": 20},
                                                                   {"path": "/b", "gb": 3, "days": 20},
                                                                   {"path": "/c", "gb": 1, "days": 20}]), \
                mock.patch.object(diskcare, "offload", side_effect=lambda p: f"moved {p}") as off, \
                mock.patch.object(config, "DISK_OFFLOAD_TO", str(self.drive)):
            state = diskcare.step({}, time.time())
        self.assertEqual([c.args[0] for c in off.call_args_list], ["/a", "/b"])  # stopped under 80%
        self.assertEqual(state["level"], "ok")
        self.assertTrue(any(a["text"].startswith("cleaned") for a in state["actions"]))


if __name__ == "__main__":
    unittest.main()

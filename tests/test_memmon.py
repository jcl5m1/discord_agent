"""Process recorder: memory, swap, pressure, top processes by memory and CPU, events.

    .venv/bin/python -m unittest tests.test_memmon
"""

import csv
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from harness import memmon


@unittest.skipUnless(sys.platform.startswith("linux"), "reads /proc")
class MemmonTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = mock.patch.object(memmon, "DIR", Path(tmp.name))
        p.start()
        self.addCleanup(p.stop)

    def test_sample_write_and_report(self):
        rec = memmon.Recorder()
        rec.processes()
        end = time.time() + 0.3
        while time.time() < end:  # use some CPU, so this process shows up
            sum(range(10000))
        row = rec.sample()
        self.assertEqual(len(row), len(memmon.HEADER))
        top_mem, top_cpu = json.loads(row[6]), json.loads(row[7])
        self.assertTrue(top_mem and {"pid", "rss_mib", "swap_mib", "cmd"} <= set(top_mem[0]))
        self.assertTrue(any(p["pid"] == __import__("os").getpid() for p in top_cpu))
        memmon.write(row)
        memmon.write(row)
        with next(memmon.DIR.glob("*.csv")).open() as fh:
            rows = list(csv.reader(fh))
        self.assertEqual((rows[0], len(rows)), (memmon.HEADER, 3))
        text = "\n".join(memmon.summary_lines([], time.time()))
        self.assertIn("**Memory**", text)
        self.assertIn("GB —", text)

    def test_low_and_ok_events_once_each(self):
        events = []
        rec = memmon.Recorder(lambda ev, **kw: events.append((ev, kw)))
        top = json.dumps([{"pid": 1, "rss_mib": 9000, "swap_mib": 100, "cmd": "python big.py"}])
        row = lambda pct: ["t", str(pct), 0, "90", 1, 1, top, "[]"]  # noqa: E731
        for pct in (30, 8, 5, 12, 20, 25):
            rec.events(row(pct))
        self.assertEqual([e for e, _ in events], ["mem_low", "mem_ok"])
        self.assertIn("9100 MiB python big.py", events[0][1]["top"][0])
        text = "\n".join(memmon.summary_lines([{"ts": time.time(), "event": "mem_low", **events[0][1]}], time.time()))
        self.assertIn("Low memory", text)


if __name__ == "__main__":
    unittest.main()

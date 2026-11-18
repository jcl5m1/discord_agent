"""GPU flight recorder: samples, minute summaries, kernel errors, incidents.

    .venv/bin/python -m unittest tests.test_gpumon
"""

import csv
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from harness import gpumon


def line(t: float, power: float = 120.0) -> str:
    stamp = time.strftime("%Y/%m/%d %H:%M:%S", time.localtime(t)) + ".250"
    return f"{stamp}, {power}, {power}, 420.00, 55, N/A, 40, 1800, 9500, 80, 9000, P2, 4, 16, 0x0000000000000000"


class RecorderTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = mock.patch.object(gpumon, "DIR", Path(tmp.name))
        p.start()
        self.addCleanup(p.stop)
        self.events = []
        self.rec = gpumon.Recorder(lambda ev, **kw: self.events.append((ev, kw)) or kw)
        self.t0 = (int(time.time()) // 60) * 60 - 600  # a minute boundary, 10 min ago

    def test_samples_and_minute_summary(self):
        for i in range(70):
            self.rec.sample(line(self.t0 + i, power=100 + i))
        self.rec.flush()
        rows = list(csv.reader(gpumon._hour_file(self.t0 + 1).open()))
        self.assertEqual(rows[0], gpumon.HEADER)
        self.assertGreaterEqual(len(rows), 2)
        minutes = list(csv.reader(next(gpumon.DIR.glob("minutes-*.csv")).open()))
        self.assertEqual(minutes[1][1:5], ["60", "100", "130", "159"])  # samples, min, mean, max

    def test_xid_logs_one_event_and_one_incident_with_samples(self):
        for i in range(30):
            self.rec.sample(line(self.t0 + i))
        t = self.t0 + 30
        with mock.patch.object(gpumon, "_gpu_processes", return_value=[]), \
                mock.patch.object(gpumon.subprocess, "run",
                                  return_value=mock.Mock(stdout="lost", stderr="")):
            self.rec.kernel_line("NVRM: Xid (PCI:0000:01:00): 79, GPU has fallen off the bus.", t=t)
            self.rec.kernel_line("NVRM: GPU 0000:01:00.0: GPU has fallen off the bus.", t=t)
            self.rec.kernel_line("NVRM: Xid (PCI:0000:01:00): 79, GPU has fallen off the bus.", t=t + 5)
            self.rec.kernel_line("some unrelated kernel line", t=t)
        xids = [kw for ev, kw in self.events if "xid" in kw]
        self.assertEqual(len(xids), 1)
        self.assertEqual((xids[0]["xid"], xids[0]["what"]), (79, "fell off the bus"))
        (incident,) = (gpumon.DIR / "incidents").iterdir()
        self.assertEqual(len(list(csv.reader((incident / "samples.csv").open()))), 31)
        self.assertEqual(len((incident / "kernel.log").read_text().splitlines()), 3)

    def test_past_error_without_samples_is_only_an_event(self):
        self.rec.kernel_line("NVRM: Xid (PCI:0000:01:00): 79, GPU has fallen off the bus.",
                             t=self.t0 - 86400 * 3, live=False)
        self.assertEqual(len(self.events), 1)
        self.assertFalse((gpumon.DIR / "incidents").exists() and
                         any((gpumon.DIR / "incidents").iterdir()))

    def test_summary_lines(self):
        now = time.time()
        events = [{"ts": now - 3600, "event": "gpu_error", "xid": 79, "what": "fell off the bus"}]
        text = "\n".join(gpumon.summary_lines(events, now))
        self.assertIn("GPU errors", text)
        self.assertIn("Xid 79 (fell off the bus)", text)
        self.assertEqual(gpumon.summary_lines([], now), [])


if __name__ == "__main__":
    unittest.main()

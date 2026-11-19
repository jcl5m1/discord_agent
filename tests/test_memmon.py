"""Memory recorder: a sample has memory, swap, pressure and the top processes.

    .venv/bin/python -m unittest tests.test_memmon
"""

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import memmon


@unittest.skipUnless(sys.platform.startswith("linux"), "reads /proc")
class MemmonTests(unittest.TestCase):
    def test_sample_and_write(self):
        row = memmon.sample()
        self.assertEqual(len(row), len(memmon.HEADER))
        self.assertGreater(float(row[1]), 0)
        top = json.loads(row[-1])
        self.assertTrue(top and {"pid", "rss_mib", "swap_mib", "cmd"} <= set(top[0]))
        self.assertGreaterEqual(top[0]["rss_mib"] + top[0]["swap_mib"], top[-1]["rss_mib"] + top[-1]["swap_mib"])
        with tempfile.TemporaryDirectory() as d, mock.patch.object(memmon, "DIR", Path(d)):
            memmon.write(row)
            memmon.write(row)
            (f,) = Path(d).glob("*.csv")
            with f.open() as fh:
                rows = list(csv.reader(fh))
        self.assertEqual(rows[0], memmon.HEADER)
        self.assertEqual(len(rows), 3)


if __name__ == "__main__":
    unittest.main()

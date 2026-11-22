"""GPU watch: when a lost GPU leads to a reboot, an alert, or nothing.

No real reboot: the commands are replaced.

    .venv/bin/python -m unittest tests.test_gpuguard
"""

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from harness import config, gpuguard


class DecideTests(unittest.TestCase):
    def test_rules(self):
        now = time.time()
        with mock.patch.object(config, "GPU_AUTO_REBOOT", True), mock.patch.object(config, "GPU_REBOOT_MIN_GAP", 7200):
            self.assertEqual(gpuguard.decide(30, {}, now, True), "wait")  # not confirmed yet
            self.assertEqual(gpuguard.decide(300, {}, now, True), "reboot")
            self.assertEqual(gpuguard.decide(300, {}, now, False), "alert")  # no permission
            self.assertEqual(gpuguard.decide(300, {"last_reboot": now - 600}, now, True), "alert")  # too soon
            self.assertEqual(gpuguard.decide(300, {"last_reboot": now - 8000}, now, True), "reboot")
        with mock.patch.object(config, "GPU_AUTO_REBOOT", False):
            self.assertEqual(gpuguard.decide(300, {}, now, True), "alert")


class RunTests(unittest.IsolatedAsyncioTestCase):
    async def run_guard(self, answers=None, seconds=1.5, back_after_warning=False):
        answers = iter(answers or [])
        posts, told, reboots, events = [], [], [], []

        def ok():
            if back_after_warning:
                return bool(told)  # lost until the agents are warned, then it answers again
            return next(answers, False)

        def fake_run(cmd, **kw):
            if cmd[:2] == ["sudo", "-n"] and "reboot" in cmd:
                reboots.append(cmd)
            return mock.Mock(returncode=0)

        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(gpuguard, "STATE", Path(d) / "guard.json"), \
                mock.patch.object(gpuguard, "CHECK_EVERY", 0.05), mock.patch.object(gpuguard, "CONFIRM", 0.1), \
                mock.patch.object(config, "GPU_AUTO_REBOOT", True), mock.patch.object(config, "GPU_REBOOT_DELAY", 0.2), \
                mock.patch.object(config, "GPU_MONITOR", True), \
                mock.patch.object(gpuguard.shutil, "which", return_value="/usr/bin/nvidia-smi"), \
                mock.patch.object(gpuguard, "gpu_ok", side_effect=ok), \
                mock.patch.object(gpuguard, "may_reboot", return_value=True), \
                mock.patch.object(gpuguard, "_discord", side_effect=posts.append), \
                mock.patch.object(gpuguard, "_tell_agents", side_effect=told.append), \
                mock.patch.object(gpuguard.subprocess, "run", side_effect=fake_run):
            stop = asyncio.Event()
            task = asyncio.ensure_future(gpuguard.run(stop, lambda ev, **kw: events.append(ev)))
            await asyncio.sleep(seconds)
            stop.set()
            await asyncio.wait_for(task, 5)
            state = gpuguard._load()
        return posts, told, reboots, events, state

    async def test_lost_gpu_warns_then_reboots_once(self):
        posts, told, reboots, events, state = await self.run_guard([True, False])
        self.assertEqual(len(reboots), 1)  # the 2 h limit stops a second one
        self.assertIn("reboots at", posts[0])
        self.assertIn("Save and commit", told[0])
        self.assertIn("gpu_lost", events)
        self.assertIn("auto_reboot", events)
        self.assertIn("last_reboot", state)

    async def test_gpu_back_before_the_reboot_cancels_it(self):
        posts, told, reboots, events, _ = await self.run_guard(seconds=1.0, back_after_warning=True)
        self.assertEqual(reboots, [])
        self.assertTrue(any("cancelled" in p for p in posts))


if __name__ == "__main__":
    unittest.main()

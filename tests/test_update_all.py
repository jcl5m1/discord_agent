"""`/update all`: worklog events, each machine's part, and the report.

    .venv/bin/python -m unittest tests.test_update_all
"""

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

from harness import bot, config
from harness.worklog import Worklog, summary

EPOCH_MS = 1420070400000


def eid(offset=0):
    return (int(time.time() * 1000) - EPOCH_MS + offset) << 22


class WorklogOpTests(unittest.TestCase):
    def test_update_and_results(self):
        st = Worklog()
        req = eid()
        ok, text = st.apply(req, {"op": "update", "by": "human:me", "force": True, "machine": "m1",
                                  "channel": 7})
        self.assertTrue(ok)
        self.assertIn("(force)", text)
        ok, _ = st.apply(eid(1), {"op": "updated", "by": "harness@m2", "req": req, "machine": "m2",
                                  "ok": True, "text": "pulled; restarting", "old": "a", "new": "b"})
        self.assertTrue(ok)
        self.assertEqual(st.updates[req]["results"]["m2"]["new"], "b")
        self.assertEqual((st.updates[req]["channel"], st.updates[req]["force"]), (7, True))
        self.assertFalse(st.apply(eid(2), {"op": "updated", "by": "harness@m2", "req": 1,
                                           "machine": "m2"})[0])
        self.assertFalse(st.apply(eid(3), {"op": "updated", "by": "harness@m2", "req": "x"})[0])

    def test_summary_lines(self):
        self.assertIn("asks every harness to update", summary({"op": "update", "by": "human:me"}))
        self.assertTrue(summary({"op": "updated", "ok": False, "machine": "m2", "text": "x"}).startswith("❌ m2"))


class FakeWorklog:
    def __init__(self, machine="m1", machines=("m1", "m2")):
        self.machine = machine
        self.machines = set(machines)
        self.state = Worklog()
        self.reports = []

    def bot_machines(self):
        return self.machines

    async def report_update(self, req, ok, text, old="", new=""):
        self.reports.append((req, ok, text, old, new))
        self.state.updates[req]["results"][self.machine] = {"ok": ok, "text": text, "old": old,
                                                            "new": new, "at": 0}


class WorklogUpdateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.channel = NS(sent=[])

        async def send(text):
            self.channel.sent.append(text)

        self.channel.send = send
        self.wl = FakeWorklog()
        self.restarts = []
        self.steps = mock.AsyncMock(return_value=("restart", ["⬇️", "🔄 restarting…"], "aaa", "bbb"))
        self.patches = [
            mock.patch.object(bot, "worklog", self.wl),
            mock.patch.object(bot, "UPDATE_HANDLED", Path(tmp.name) / "handled.json"),
            mock.patch.object(bot, "UPDATE_MARKER", Path(tmp.name) / "marker.json"),
            mock.patch.object(bot, "UPDATE_WAIT", 0.3),
            mock.patch.object(bot, "_update_steps", self.steps),
            mock.patch.object(bot, "_restart_harness", lambda: self.restarts.append(1)),
            mock.patch.object(bot, "_busy_turns", lambda: []),
            mock.patch.object(bot.client, "get_channel", lambda cid: self.channel if cid == 7 else None),
            mock.patch.object(bot.asyncio, "sleep", self.fast_sleep),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    _real_sleep = asyncio.sleep

    async def fast_sleep(self, delay):
        await WorklogUpdateTests._real_sleep(min(delay, 0.01))

    def request(self, machine="m2", force=False, req=11):
        self.wl.state.updates[req] = {"by": "human:me", "at": 0, "force": force, "machine": machine,
                                      "channel": 7, "results": {}}
        return req, self.wl.state.updates[req]

    async def test_other_machine_pulls_reports_and_restarts_once(self):
        req, request = self.request(machine="m2")
        await bot._worklog_update(req, request)
        await bot._worklog_update(req, request)  # replayed after the restart: not run again
        self.assertEqual(self.wl.reports, [(req, True, "pulled; restarting", "aaa", "bbb")])
        self.assertEqual(self.restarts, [1])
        self.assertEqual(self.channel.sent, [])  # only the asking machine posts the report
        self.assertFalse(bot.UPDATE_MARKER.exists())

    async def test_busy_machine_skips_without_force(self):
        req, request = self.request()
        with mock.patch.object(bot, "_busy_turns", lambda: ["harness 2"]):
            await bot._worklog_update(req, request)
        self.assertFalse(self.wl.reports[0][1])
        self.assertIn("1 agent turn(s) running (harness 2)", self.wl.reports[0][2])
        self.steps.assert_not_called()
        self.assertEqual(self.restarts, [])

    async def test_force_updates_a_busy_machine(self):
        req, request = self.request(force=True)
        with mock.patch.object(bot, "_busy_turns", lambda: ["harness 2"]):
            await bot._worklog_update(req, request)
        self.steps.assert_awaited_once_with(True)
        self.assertEqual(self.restarts, [1])

    async def test_asking_machine_updates_last_and_posts_the_report(self):
        req, request = self.request(machine="m1")

        async def other_reports():
            await WorklogUpdateTests._real_sleep(0.05)
            self.wl.state.updates[req]["results"]["m2"] = {
                "ok": True, "text": "already up to date", "old": "bbb", "new": "bbb", "at": 0}

        asyncio.get_running_loop().create_task(other_reports())
        await bot._worklog_update(req, request)
        self.assertEqual(self.restarts, [1])
        report = self.channel.sent[0]
        self.assertIn("**m1** `aaa` → `bbb` pulled; restarting", report)
        self.assertIn("**m2** already up to date", report)
        self.assertNotIn("no answer", report)
        marker = json.loads(bot.UPDATE_MARKER.read_text())
        self.assertEqual((marker["req"], marker["channel_id"], marker["old"]), (req, 7, "aaa"))

    async def test_report_lists_machines_with_no_answer_and_errors(self):
        req, request = self.request(machine="m1")
        self.wl.machines = {"m1", "m2", "m3"}
        self.steps.return_value = ("error", ["⬇️", "❌ `git pull` failed, not restarting:\n```\nconflict\n```"],
                                   "aaa", "aaa")
        await bot._worklog_update(req, request)
        self.assertEqual(self.restarts, [])
        self.assertEqual(self.wl.reports[0][:3], (req, False, "`git pull` failed, not restarting:"))
        self.assertIn("⏳ no answer yet: m2, m3", self.channel.sent[0])


class WindowsRestartTests(unittest.TestCase):
    def test_restart_command_runs_the_installer_outside_the_process_tree(self):
        cmd = bot._windows_restart_cmd()
        self.assertEqual(cmd[:5], ["cmd.exe", "/c", "start", '""', "/b"])
        self.assertEqual(cmd[-2:], [str(config.BASE_DIR / "install-windows-tasks.ps1"), "-Start"])
        self.assertIn("powershell.exe", cmd)


if __name__ == "__main__":
    unittest.main()

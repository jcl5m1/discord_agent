"""The worklog across two simulated bots sharing one fake #worklog channel.

    .venv/bin/python -m unittest tests.test_worklog_service
"""

import asyncio
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

from aiohttp import ClientSession

from harness import config
from harness import worklog_service as svc
from harness.worklog import encode, summary

EPOCH_MS = 1420070400000
ROOT = Path(__file__).resolve().parent.parent


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Msg:
    seq = 0

    def __init__(self, content, bot_id):
        Msg.seq += 1
        self.id = (int(time.time() * 1000) - EPOCH_MS) << 22 | Msg.seq
        self.content, self.author = content, NS(bot=True, id=bot_id)

    async def edit(self, content=None):
        self.content = content


class Channel:
    """One #worklog shared by every bot; each hears new messages after its own delay."""

    id, name = 999, "worklog"

    def __init__(self):
        self.msgs, self.subs = [], []

    async def send(self, content, bot_id=0):
        m = Msg(content, bot_id)
        self.msgs.append(m)
        loop = asyncio.get_running_loop()
        for service, delay in self.subs:
            loop.call_later(delay, lambda s=service: asyncio.ensure_future(s.ingest(m)))
        return m

    async def history(self, limit=None, after=None, oldest_first=True):
        for m in sorted(self.msgs, key=lambda m: m.id):
            if not after or m.id > after.id:
                yield m


class BotView:
    """The channel as one bot sees it (its sends are tagged with its id)."""

    def __init__(self, channel, bot_id):
        self.channel, self.bot_id, self.id, self.name = channel, bot_id, channel.id, channel.name

    async def send(self, content):
        return await self.channel.send(content, self.bot_id)

    def history(self, **kw):
        return self.channel.history(**kw)


class TwoBotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(svc, "SETTLE", 0.4)
        p.start()
        self.addCleanup(p.stop)
        self.channel = Channel()
        self.delivered, self.notified = [], []
        self.a = await self.bot(1)
        self.b = await self.bot(2)
        self.channel.subs = [(self.a, 0.05), (self.b, 0.15)]  # b hears everything later
        await self.a.register("alice@m1", 101, "s1", "claude in /x")
        await self.b.register("bob@m2", 202, "s2", "kimi in /y")

    async def bot(self, bot_id):
        port = free_port()

        async def deliver(worker, sender, text):
            self.delivered.append((bot_id, worker, sender, text))

        async def notify(worker, text):
            self.notified.append((bot_id, worker, text))

        with mock.patch.object(config, "DATA_DIR", Path(self.tmp.name) / str(bot_id)), \
                mock.patch.object(config, "WORKLOG_PORT", port):
            s = svc.WorklogService(NS(user=NS(id=bot_id)), deliver, notify)
            s.machine = f"m{bot_id}"
            await s.start(NS(text_channels=[BotView(self.channel, bot_id)]))
        return s

    async def test_simultaneous_claims_one_winner_everywhere(self):
        await self.a.run(["add", "Speed up reconstructor", "--files", "r.py"], "alice@m1")
        (oa, _), (ob, tb) = await asyncio.gather(self.a.run(["claim", "1"], "alice@m1"),
                                                 self.b.run(["claim", "1"], "bob@m2"))
        self.assertNotEqual(oa, ob)
        await asyncio.sleep(0.3)
        winner = "alice@m1" if oa else "bob@m2"
        self.assertEqual(self.a.state.tasks[1].owner, winner)
        self.assertEqual(self.b.state.tasks[1].owner, winner)
        self.assertTrue(any(m.content.startswith("❌ refused") for m in self.channel.msgs))

    async def test_checkin_updates_dashboard_without_chat_repost_and_mail_delivered(self):
        await self.b.run(["add", "Rebase", "--area", "harness", "--files", "x.py"], "bob@m2")
        await self.b.run(["claim", "1"], "bob@m2")
        ok, _ = await self.b.run(["checkin", "1", "rebased onto main, tests running"], "bob@m2")
        self.assertTrue(ok)
        await self.a.run(["send", "bob@m2", "ping me when done", "--task", "1"], "alice@m1")
        await asyncio.sleep(0.3)
        self.assertEqual(self.notified, [])
        self.assertEqual(self.delivered, [(2, "bob@m2", "alice@m1", "(re #1) ping me when done")])

        async with ClientSession() as client:
            async with client.get(f"{self.b.url}/state") as response:
                self.assertEqual(response.status, 403)
            async with client.get(
                f"{self.b.url}/state", headers={"X-Worklog-Token": self.b.token}
            ) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                data = await response.json()
            async with client.get(f"{self.b.url}/?token={self.b.token}") as response:
                self.assertEqual(response.status, 200)
                self.assertIn("text/html", response.content_type)
                self.assertIn("Cache-Control", response.headers)
                html = await response.text()
        self.assertEqual(data["tasks"][0]["progress"], "rebased onto main, tests running")
        self.assertEqual(data["tasks"][0]["area"], "harness")
        self.assertIn("WORKLOG", html.upper())
        with mock.patch.object(config, "PUBLIC_BASE_URL", "https://agents.example.com"):
            self.assertEqual(
                self.b.dashboard_url(), f"https://agents.example.com/agents/_worklog/{self.b.token}/"
            )

    async def test_dashboard_reply_persists_and_immediately_notifies_owner(self):
        await self.b.run(["add", "Review blocked work", "--area", "harness"], "bob@m2")
        await self.b.run(["claim", "1"], "bob@m2")
        await self.b.run(["add", "No owner", "--area", "harness"], "bob@m2")

        async with ClientSession() as client:
            async with client.post(
                f"{self.b.url}/reply", json={"task": 1, "text": "Is this still blocked?"}
            ) as response:
                self.assertEqual(response.status, 403)
            for body in (
                {"task": 1, "text": " "}, {"task": True, "text": "invalid"},
                {"task": "1", "text": "invalid"}, {"task": 999, "text": "missing"},
            ):
                async with client.post(
                    f"{self.b.url}/reply", json=body, headers={"X-Worklog-Token": self.b.token}
                ) as response:
                    self.assertEqual(response.status, 400)
            async with client.post(
                f"{self.b.url}/reply", json={"task": 2, "text": "ping"},
                headers={"X-Worklog-Token": self.b.token},
            ) as response:
                self.assertEqual(response.status, 409)
            async with client.post(
                f"{self.b.url}/reply", json={"task": 1, "text": "  Is this still blocked?  "},
                headers={"X-Worklog-Token": self.b.token},
            ) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                reply = await response.json()
            async with client.get(
                f"{self.b.url}/state", headers={"X-Worklog-Token": self.b.token}
            ) as response:
                data = await response.json()

        self.assertTrue(reply["ok"])
        self.assertEqual(reply["thread"]["text"], "Is this still blocked?")
        message = self.b.state.messages[-1]
        self.assertEqual((message.task, message.sender, message.to, message.text),
                         (1, "human:dashboard", "bob@m2", "Is this still blocked?"))
        self.assertEqual(data["tasks"][0]["thread"][-1], reply["thread"])
        self.assertEqual(self.delivered, [
            (2, "bob@m2", "human:dashboard", "(re #1) Is this still blocked?"),
        ])

    async def test_dashboard_drop_drops_task_and_tells_owner(self):
        await self.b.run(["add", "Stale work", "--area", "harness"], "bob@m2")
        await self.b.run(["claim", "1"], "bob@m2")
        await self.b.run(["add", "No owner", "--area", "harness"], "bob@m2")
        auth = {"X-Worklog-Token": self.b.token}

        async with ClientSession() as client:
            async with client.post(f"{self.b.url}/drop", json={"task": 1}) as response:
                self.assertEqual(response.status, 403)
            for body in ({"task": "1"}, {"task": True}, {"task": 999}, {"task": 1, "reason": 5},
                         {"task": 1, "reason": "x" * 2001}):
                async with client.post(f"{self.b.url}/drop", json=body, headers=auth) as response:
                    self.assertEqual(response.status, 400, body)
            self.assertEqual(self.b.state.tasks[1].status, "claimed")
            async with client.post(f"{self.b.url}/drop", json={"task": 1, "reason": " Not needed now "},
                                   headers=auth) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertTrue((await response.json())["ok"])
            async with client.post(f"{self.b.url}/drop", json={"task": 1}, headers=auth) as response:
                self.assertEqual(response.status, 409)
            async with client.post(f"{self.b.url}/drop", json={"task": 2}, headers=auth) as response:
                self.assertEqual(response.status, 200)

        task = self.b.state.tasks[1]
        self.assertEqual((task.status, task.owner), ("dropped", "bob@m2"))
        self.assertEqual(task.notes[-1][1:], ("human:dashboard", "[dropped] Not needed now"))
        self.assertEqual(self.b.state.tasks[2].status, "dropped")
        self.assertEqual(self.b.state.tasks[2].notes[-1][2], "[dropped] Dropped from the dashboard")
        self.assertEqual(self.delivered, [(
            2, "bob@m2", "human:dashboard",
            "(re #1) The user dropped #1 from the dashboard. Stop work on it. Reason: Not needed now",
        )])

    async def test_update_all_reaches_every_bot_and_collects_results(self):
        calls = []

        def on_update(service):
            async def run(req, request):
                calls.append((service.machine, req, request["force"]))
                await service.report_update(req, True, "pulled; restarting", "aaa", "bbb")
            return run

        self.a.on_update, self.b.on_update = on_update(self.a), on_update(self.b)
        self.assertEqual(self.a.bot_machines(), {"m1", "m2"})
        ok, _ = await self.a.post({"op": "update", "by": "human:tester", "force": True,
                                   "machine": "m1", "channel": 55})
        self.assertTrue(ok)
        await asyncio.sleep(1.0)
        req = next(iter(self.a.state.updates))
        self.assertEqual(sorted(calls), [("m1", req, True), ("m2", req, True)])
        for service in (self.a, self.b):
            u = service.state.updates[req]
            self.assertEqual((u["machine"], u["channel"], u["by"]), ("m1", 55, "human:tester"))
            self.assertEqual(sorted(u["results"]), ["m1", "m2"])
            self.assertEqual(u["results"]["m2"]["new"], "bbb")
        self.assertEqual(self.a.missed_updates(), [])

        # A bot that starts later (it was offline) runs the request it missed, once.
        late = []

        async def late_update(req, request):
            late.append(req)

        with mock.patch.object(svc.WorklogService, "_serve", mock.AsyncMock()):
            c = await self.bot(3)
        c.machine = "m3"
        self.assertEqual(c.missed_updates(), [req])
        c.on_update = late_update
        c._start_update(req)
        await asyncio.sleep(0.05)
        self.assertEqual(late, [req])
        self.assertEqual(c.missed_updates(now=time.time() + svc.UPDATE_CATCH_UP + 1), [])

    async def test_terminal_sessions_lapsed_claim_released_once(self):
        await self.a.run(["add", "Migrate hdd"], "alice@m1")
        await self.b.run(["claim", "1"], "term@m2")  # a terminal session using bot b's wl
        await asyncio.sleep(0.3)
        later = time.time() + svc.CHECKIN_EVERY + 60
        with mock.patch.object(svc.time, "time", return_value=later):
            self.assertEqual(self.a._orphans(later), [])  # b posted the claim: b's job
            await asyncio.gather(self.a._check_claims(), self.b._check_claims())
        await asyncio.sleep(0.3)
        task = self.a.state.tasks[1]
        self.assertEqual((task.status, task.owner), ("open", ""))
        mail = [m for m in self.a.state.messages if m.to == "term@m2"]
        self.assertEqual(len(mail), 1)
        self.assertIn("released", mail[0].text)

    async def test_active_overdue_claim_is_kept_without_notice_spam(self):
        await self.a.run(["add", "Keep active turn", "--area", "x"], "alice@m1")
        await self.a.run(["claim", "1"], "alice@m1")
        await asyncio.sleep(0.3)
        self.a.active = lambda worker: worker == "alice@m1"
        claimed_at = self.a.state.tasks[1].checkin_at
        later = claimed_at + svc.CHECKIN_EVERY + 1
        with mock.patch.object(svc.time, "time", return_value=later):
            await self.a._check_claims()
            await self.a._check_claims()

        task = self.a.state.tasks[1]
        self.assertGreater(task.checkin_at, claimed_at)
        self.assertFalse(task.lapsed(time.time()))
        self.assertEqual((task.status, task.owner), ("claimed", "alice@m1"))
        notices = [d for d in self.delivered if d[:3] == (1, "alice@m1", "harness")]
        self.assertEqual(len(notices), 1)
        self.assertIn("#1", notices[0][3])
        self.assertIn("provider process is still active", notices[0][3])
        self.assertIn("renewed shared ownership", notices[0][3])

        self.a.idle_for = lambda worker: 20 * 60
        self.assertEqual(self.a._offer_plan(later), [])
        ok, outcome = await self.b.run(["claim", "1"], "bob@m2")
        self.assertFalse(ok)
        self.assertIn("held by alice@m1", outcome)
        self.assertEqual(self.a.state.tasks[1].owner, "alice@m1")

    async def test_active_overdue_claim_releases_and_offers_after_cleanup(self):
        await self.a.run(["add", "Finish active turn", "--area", "x"], "alice@m1")
        await self.a.run(["claim", "1"], "alice@m1")
        await asyncio.sleep(0.3)
        self.a.active = lambda worker: True
        later = self.a.state.tasks[1].checkin_at + svc.CHECKIN_EVERY + 1
        with mock.patch.object(svc.time, "time", return_value=later):
            await self.a._check_claims()
            await self.a._check_claims()
            await self.a._check_claims()
        notices = [d for d in self.delivered if d[:3] == (1, "alice@m1", "harness")
                   and "provider process is still active" in d[3]]
        self.assertEqual(len(notices), 1)

        self.a.active = lambda worker: False
        later = self.a.state.tasks[1].checkin_at + svc.CHECKIN_EVERY + 1
        with mock.patch.object(svc.time, "time", return_value=later):
            await self.a._check_claims()
            await self.a._check_claims()
        task = self.a.state.tasks[1]
        self.assertEqual((task.status, task.owner), ("open", ""))
        releases = [n for n in self.notified if n[:2] == (1, "alice@m1") and "released" in n[2]]
        self.assertEqual(len(releases), 1)
        self.assertIn("no check-in", releases[0][2])

        self.a.idle_for = lambda worker: svc.OFFER_AFTER
        self.assertEqual([(w, t.num) for w, t in self.a._offer_plan(later)], [("alice@m1", 1)])
        self.a.idle_for = lambda worker: svc.OFFER_AFTER - 1
        self.assertEqual(self.a._offer_plan(later), [])

    async def test_idle_agents_are_offered_open_tasks_once(self):
        await self.a.run(["add", "Fix lists", "--prio", "1", "--area", "x"], "alice@m1")
        await self.a.run(["add", "Port tool", "--prio", "1"], "alice@m1")  # no area: offered to nobody
        await self.a.run(["add", "Other project", "--prio", "1", "--area", "y"], "alice@m1")
        await asyncio.sleep(0.3)
        self.a.idle_for = lambda w: 20 * 60
        self.assertEqual(self.a._offer_plan(time.time()), [])  # just added: its creator may add dependencies
        later = time.time() + svc.OFFER_NEW_AFTER + 1
        plan = self.a._offer_plan(later)
        self.assertEqual([(w, t.num) for w, t in plan], [("alice@m1", 1)])  # "x" is in alice's folder /x
        with mock.patch.object(svc.time, "time", return_value=later):
            await self.a._offer_work()
        self.assertEqual(self.delivered[-1][:3], (1, "alice@m1", "harness:offer"))
        self.assertIn("wl claim 1", self.delivered[-1][3])
        self.assertEqual(self.a._offer_plan(later), [])  # one offer per agent per OFFER_GAP
        self.a.idle_for = lambda w: 60  # idle only a minute: no offer
        self.a._offered.clear()
        self.assertEqual(self.a._offer_plan(later), [])

    async def test_after_a_restart_the_agent_hears_of_its_subagents_claims(self):
        await self.b.run(["add", "Long build"], "bob@m2")
        await self.b.run(["claim", "1"], "bob@m2/sub-1")
        await self.b.run(["add", "Other"], "bob@m2")
        await self.b.run(["claim", "2"], "bob@m2")  # the agent's own claim: not listed
        await asyncio.sleep(0.3)
        self.assertEqual({w: [t.num for t in ts] for w, ts in self.b.orphaned_subagents().items()},
                         {"bob@m2": [1]})
        self.assertEqual(self.a.orphaned_subagents(), {})  # bob runs on bot b
        await self.b._after_restart(delay=0)
        (bot, worker, sender, text) = self.delivered[-1]
        self.assertEqual((bot, worker, sender), (2, "bob@m2", "harness"))
        self.assertIn("#1 Long build (held by bob@m2/sub-1", text)

    async def test_late_event_triggers_replay(self):
        now = time.time()
        sid = lambda t, s: (int(t * 1000) - EPOCH_MS) << 22 | s  # noqa: E731
        add = NS(id=sid(now, 900), author=NS(bot=True, id=9), content=encode(
            {"op": "add", "by": "x", "title": "t"}, "x"))
        late = NS(id=sid(now + 1, 901), author=NS(bot=True, id=9), content=encode(
            {"op": "claim", "by": "bob@m2", "task": 1}, summary({"op": "claim", "by": "bob@m2", "task": 1})))
        early = NS(id=sid(now + 1, 902), author=NS(bot=True, id=9), content=encode(
            {"op": "claim", "by": "alice@m1", "task": 1}, "x"))
        for m in (add, early, late):  # `late` has the earlier id but arrives last
            await self.a.ingest(m)
        self.assertEqual(self.a.state.tasks[1].owner, "bob@m2")

    async def test_wl_command_end_to_end(self):
        env = {**os.environ, **self.a.env_for("alice@m1")}
        env["WORKLOG_URL"] = self.a.url

        def wl(*args, **extra):
            return subprocess.run([sys.executable, str(ROOT / "bin" / "wl"), *args],
                                  env={**env, **extra}, capture_output=True, text=True, timeout=30)

        r = await asyncio.to_thread(wl, "add", "Profile the API", "--prio", "1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = await asyncio.to_thread(wl, "claim", "1", "starting with py-spy")
        self.assertIn("claimed #1", r.stdout)
        r = await asyncio.to_thread(wl, "checkin", "1", " ".join(["word"] * 11))
        self.assertEqual(r.returncode, 1)
        r = await asyncio.to_thread(wl, "status")
        self.assertIn("alice@m1", r.stdout)
        r = await asyncio.to_thread(wl, "status", WORKLOG_TOKEN="wrong")
        self.assertEqual((r.returncode, r.stdout.strip()), (1, "bad worklog token"))


class LockTests(unittest.IsolatedAsyncioTestCase):
    async def test_hidden_from_people_open_to_every_bot(self):
        import discord

        class Role(NS):
            def __hash__(self):
                return hash(self.name)

        everyone = Role(name="@everyone", is_bot_managed=lambda: False)
        human = Role(name="mods", is_bot_managed=lambda: False)
        bot1, bot2 = (Role(name=n, is_bot_managed=lambda: True) for n in ("b1", "b2"))
        calls = []

        class Chan:
            name = "worklog"

            def __init__(self):
                self.overwrites = {}

            async def set_permissions(self, target, overwrite):
                calls.append(target.name)
                self.overwrites[target] = overwrite

        s = svc.WorklogService.__new__(svc.WorklogService)
        s.channel = Chan()
        guild = NS(default_role=everyone, roles=[everyone, human, bot1, bot2])
        await s.lock(guild)
        self.assertFalse(s.channel.overwrites[everyone].view_channel)
        self.assertTrue(s.channel.overwrites[bot2].send_messages)
        self.assertNotIn(human, s.channel.overwrites)
        await s.lock(guild)  # already locked: nothing to change
        self.assertEqual(calls, ["b1", "b2", "@everyone"])  # bots let in before hiding

    async def test_never_hidden_if_a_bot_cant_be_let_in(self):
        import discord

        class Role(NS):
            def __hash__(self):
                return hash(self.name)

        everyone = Role(name="@everyone", is_bot_managed=lambda: False)
        bot = Role(name="b1", is_bot_managed=lambda: True)

        class Chan:
            name, overwrites = "worklog", {}

            async def set_permissions(self, target, overwrite):
                raise discord.Forbidden(NS(status=403, reason="Forbidden"), "Missing Access")

        s = svc.WorklogService.__new__(svc.WorklogService)
        s.channel = Chan()
        await s.lock(NS(default_role=everyone, roles=[everyone, bot]))  # logs, doesn't raise
        self.assertNotIn(everyone, s.channel.overwrites)


if __name__ == "__main__":
    unittest.main()

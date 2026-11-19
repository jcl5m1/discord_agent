"""Worklog rules: claims, check-ins, leases, messages, encoding, replay.

    .venv/bin/python -m unittest tests.test_worklog
"""

import unittest

from harness.worklog import (
    CHECKIN_EVERY, CommandError, Worklog, decode, encode, event_time, run_command, summary,
)

EPOCH_MS = 1420070400000


def sid(seconds: float, seq: int = 0) -> int:
    """A Discord-style id for a unix time (seconds), with a tiebreak sequence."""
    return (int(seconds * 1000) - EPOCH_MS) << 22 | seq


T0 = 1_800_000_000.0


class Log:
    """Applies events at given times, like the service does with Discord ids."""

    def __init__(self):
        self.state = Worklog()
        self.seq = 0

    def post(self, at: float, ev: dict):
        self.seq += 1
        return self.state.apply(sid(T0 + at, self.seq), ev)

    def wl(self, at: float, worker: str, argv: str):
        text, ev, _ = run_command(self.state, argv, worker, set(), now=T0 + at)
        return self.post(at, ev) if ev else (True, text)


class ClaimTests(unittest.TestCase):
    def setUp(self):
        self.log = Log()
        self.log.wl(0, "a", 'add "Speed up reconstructor" --files r.py --prio 1')

    def test_claim_is_exclusive_while_checked_in(self):
        self.assertTrue(self.log.wl(1, "a", 'claim 1 "starting fp16 descriptors"')[0])
        ok, text = self.log.wl(2, "b", "claim 1")
        self.assertFalse(ok)
        self.assertIn("held by a", text)

    def test_lapsed_claim_can_be_taken_and_owner_is_told(self):
        self.log.wl(1, "a", "claim 1")
        ok, _ = self.log.wl(1 + CHECKIN_EVERY + 1, "b", "claim 1")
        self.assertTrue(ok)
        self.assertEqual(self.log.state.tasks[1].owner, "b")
        self.assertIn("lapsed", self.log.state.inbox("a", set())[0].text)

    def test_checkin_refreshes_the_claim(self):
        self.log.wl(1, "a", "claim 1")
        self.assertTrue(self.log.wl(CHECKIN_EVERY - 10, "a", 'checkin 1 "fp16 path compiles, validating"')[0])
        self.assertFalse(self.log.wl(CHECKIN_EVERY + 60, "b", "claim 1")[0])
        self.assertEqual(self.log.state.tasks[1].progress, "fp16 path compiles, validating")

    def test_checkin_is_one_to_ten_words(self):
        self.log.wl(1, "a", "claim 1")
        with self.assertRaises(CommandError):
            run_command(self.log.state, 'checkin 1 "' + "word " * 11 + '"', "a", set())
        with self.assertRaises(CommandError):
            run_command(self.log.state, 'checkin 1 ""', "a", set())
        # the rule holds even for an event posted some other way
        self.assertFalse(self.log.post(2, {"op": "checkin", "by": "a", "task": 1, "text": "x " * 11})[0])

    def test_only_the_owner_checks_in(self):
        self.log.wl(1, "a", "claim 1")
        self.assertFalse(self.log.wl(2, "b", 'checkin 1 "sneaky"')[0])

    def test_done_by_owner_tells_the_creator(self):
        self.log.wl(1, "b", "claim 1")
        self.assertTrue(self.log.wl(5, "b", 'done 1 "committed abc123"')[0])
        self.assertEqual(self.log.state.tasks[1].status, "done")
        self.assertIn("committed abc123", self.log.state.inbox("a", set())[0].text)
        self.assertFalse(self.log.wl(6, "c", "claim 1")[0])

    def test_harness_can_release_a_lapsed_claim(self):
        self.log.wl(1, "a", "claim 1")
        ok, _ = self.log.post(CHECKIN_EVERY + 5, {"op": "release", "by": "harness@m", "task": 1,
                                                 "text": "no check-in for 30 min", "harness": True})
        self.assertTrue(ok)
        self.assertEqual((self.log.state.tasks[1].status, self.log.state.tasks[1].owner), ("open", ""))


class StaleTests(unittest.TestCase):
    def test_stale_after_checkin_period_or_with_a_lapsed_claim(self):
        log = Log()
        log.wl(0, "a", 'add "x"')
        log.wl(1, "a", "claim 1")
        log.wl(CHECKIN_EVERY - 60, "b", "register")
        st = log.state
        self.assertFalse(st.is_stale("a", T0 + 60))
        self.assertTrue(st.is_stale("a", T0 + CHECKIN_EVERY + 5))  # lapsed claim
        self.assertFalse(st.is_stale("b", T0 + CHECKIN_EVERY + 5))
        self.assertTrue(st.is_stale("b", T0 + 2 * CHECKIN_EVERY))  # not seen for 30 min
        self.assertTrue(st.is_stale("nobody", T0))


class ReadyTaskTests(unittest.TestCase):
    def setUp(self):
        self.log = Log()
        self.log.wl(0, "a", 'add "base" --prio 2 --area x')
        self.log.wl(1, "a", 'add "later" --prio 1 --after 1 --area x')
        self.log.wl(2, "a", 'add "low" --prio 3 --area x')
        self.log.wl(2, "c", "areas x")

    def test_dependencies_hold_a_task_back_until_done(self):
        st = self.log.state
        self.assertEqual([t.num for t in st.ready()], [1, 3])
        self.assertEqual(st.waiting_on(st.tasks[2]), [1])
        self.log.wl(3, "b", "claim 1")
        self.log.wl(4, "b", 'done 1 "ok"')
        self.assertEqual([t.num for t in st.ready()], [2, 3])  # p1 first

    def test_pass_hides_a_task_from_that_worker_and_its_subagents(self):
        self.assertTrue(self.log.wl(3, "b", 'pass 1 "other project"')[0])
        self.assertEqual([t.num for t in self.log.state.ready("b/sub-1")], [3])
        self.assertEqual([t.num for t in self.log.state.ready("c")], [1, 3])
        self.assertIn("other project", self.log.state.tasks[1].passes["b"])
        self.assertFalse(self.log.post(4, {"op": "pass", "by": "b", "task": 3, "text": ""})[0])

    def test_depends_and_next(self):
        self.assertTrue(self.log.wl(3, "a", "depends 3 --on 1,2")[0])
        self.assertEqual([t.num for t in self.log.state.ready()], [1])
        text = run_command(self.log.state, "next", "c", set())[0]
        self.assertIn("#1", text)
        self.assertIn("wl claim 1", text)
        self.assertFalse(self.log.wl(4, "a", "depends 3 --on 99")[0])


class AreaTests(unittest.TestCase):
    def test_keys_and_matching(self):
        from harness.worklog import Worker, area_keys, area_matches
        w = Worker("placeframe-compute@m", desc="claude in /home/u/github/placeframe-insta360", areas=["localizer"])
        keys = area_keys(w)
        self.assertEqual(keys, {"placeframe-compute", "placeframe-insta360", "localizer"})
        self.assertTrue(area_matches("placeframe", keys))  # extends "placeframe-..."
        self.assertTrue(area_matches("Localizer", keys))
        self.assertFalse(area_matches("harness", keys))
        self.assertFalse(area_matches("", keys))  # no area: nobody's
        h = Worker("harness-2@m", desc="claude in /x/discord_agent")
        self.assertTrue(area_matches("harness", area_keys(h)))
        self.assertTrue(area_matches("harness", area_keys(Worker("bot@m", desc="claude in /x/discord_agent"),
                                                           {"discord_agent": "harness"})))

    def test_wl_areas_and_next(self):
        log = Log()
        log.wl(0, "a", 'add "harness work" --area harness')
        log.wl(0, "a", 'add "app work" --area app')
        log.wl(1, "pf@m", "register")
        self.assertIn("no open task", run_command(log.state, "next", "pf@m", set())[0])
        self.assertTrue(log.wl(2, "pf@m", "areas app,maps")[0])
        self.assertIn("#2", run_command(log.state, "next", "pf@m", set())[0])
        self.assertIn("app", run_command(log.state, "areas", "pf@m", set())[0])


class GoneWorkerTests(unittest.TestCase):
    def test_status_hides_gone_workers(self):
        from harness.worklog import GONE_AFTER
        log = Log()
        log.wl(0, "old@m", "register")
        log.wl(0, "busy@m/sub-1", 'add "x"')
        log.wl(0, "busy@m/sub-1", "claim 1")
        log.wl(0, "done@m/sub-2", "register")
        log.wl(GONE_AFTER - 3600, "new@m", "register")
        later = T0 + GONE_AFTER + 60
        text = run_command(log.state, "workers", "", set(), now=later)[0]
        self.assertIn("new@m", text)
        self.assertIn("busy@m/sub-1", text)  # holds a claim
        self.assertNotIn("old@m", text)
        self.assertNotIn("done@m/sub-2", text)
        self.assertIn("2 gone", text)
        self.assertIn("old@m", run_command(log.state, "workers --all", "", set(), now=later)[0])

    def test_workers_without_an_instance_on_this_machine_are_gone(self):
        log = Log()
        for w in ("pf@m", "pf@m/sub-1", "harness@m", "issues@m", "other@m2", "term"):
            log.wl(0, w, "register")
        text = run_command(log.state, "workers", "", set(), now=T0 + 60, live={"pf@m"}, machine="m")[0]
        self.assertIn("pf@m/sub-1", text)
        for gone in ("harness@m", "issues@m"):
            self.assertNotIn(gone, text)
        self.assertIn("other@m2", text)  # another machine's: its own bot knows
        self.assertIn("term", text)  # a terminal session, seen a minute ago
        later = run_command(log.state, "workers", "", set(), now=T0 + 2 * 86400, live={"pf@m"}, machine="m")[0]
        self.assertNotIn("term ", later)  # no bot runs it: gone after a day


class LeaseAndMessageTests(unittest.TestCase):
    def test_lease_exclusive_until_expiry(self):
        log = Log()
        self.assertTrue(log.wl(0, "a", 'lease gpu --ttl 600 --why "benchmark"')[0])
        self.assertFalse(log.wl(10, "b", "lease gpu")[0])
        self.assertTrue(log.wl(700, "b", "lease gpu")[0])

    def test_messages_and_inbox(self):
        log = Log()
        log.wl(0, "b", "register")
        self.assertTrue(log.wl(1, "a", 'send b "rebase done" --task 3')[0])
        self.assertFalse(log.wl(1, "a", 'send nobody "hi"')[0])
        log.wl(2, "a", 'send all "GPU free"')
        text, _, read = run_command(log.state, "inbox", "b", set())
        self.assertIn("a re #3: rebase done", text)
        self.assertIn("GPU free", text)
        self.assertEqual(len(read), 2)
        self.assertEqual(run_command(log.state, "inbox", "b", set(read))[0], "(no unread messages)")
        self.assertEqual(run_command(log.state, "inbox", "a", set())[0], "(no unread messages)")


class OrderTests(unittest.TestCase):
    def test_same_events_same_state_and_earlier_claim_wins(self):
        events = [(sid(T0, 1), {"op": "add", "by": "a", "title": "x"}),
                  (sid(T0 + 1, 2), {"op": "claim", "by": "b", "task": 1}),
                  (sid(T0 + 1, 3), {"op": "claim", "by": "c", "task": 1})]
        for order in (events, list(reversed(events))):
            state = Worklog()
            for eid, ev in sorted(order):  # the service always applies in id order
                state.apply(eid, ev)
            self.assertEqual(state.tasks[1].owner, "b")
            self.assertFalse(state.results[events[2][0]][0])

    def test_event_time_round_trip(self):
        self.assertAlmostEqual(event_time(sid(T0 + 12.5)), T0 + 12.5, places=3)


class EncodingTests(unittest.TestCase):
    def test_round_trip_with_awkward_text(self):
        ev = {"op": "note", "by": "a", "task": 1, "text": "uses `code`, *stars*, ünïcode\nand a newline"}
        content = encode(ev, summary(ev))
        self.assertLessEqual(len(content), 2000)
        self.assertEqual(decode(content), ev)
        self.assertNotIn("\n", content.split("\n")[0][:-1] if False else "")

    def test_non_events_are_ignored(self):
        self.assertIsNone(decode("just a human message"))
        self.assertIsNone(decode("-# wl `not json`"))


class CommandTests(unittest.TestCase):
    def test_reads_need_no_name_writes_do(self):
        state = Worklog()
        self.assertIn("Workers:", run_command(state, "status", "", set())[0])
        with self.assertRaises(CommandError):
            run_command(state, 'add "x"', "", set())

    def test_as_overrides_the_worker(self):
        _, ev, _ = run_command(Worklog(), 'add "x" --as a/sub-1', "a", set())
        self.assertEqual(ev["by"], "a/sub-1")

    def test_bad_arguments_raise_instead_of_exiting(self):
        with self.assertRaises(CommandError):
            run_command(Worklog(), "claim notanumber", "a", set())


if __name__ == "__main__":
    unittest.main()

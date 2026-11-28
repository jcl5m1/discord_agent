"""Worklog dashboard snapshots and standalone page.

    .venv/bin/python -m unittest tests.test_worklog_dashboard
"""

import json
import unittest
from unittest.mock import patch

from harness.worklog import Lease, Message, Task, Worker, Worklog
from harness.worklog_dashboard import HISTORY_LIMIT, page, snapshot, todo

TASK_FIELDS = {
    "num", "title", "desc", "area", "files", "prio", "status", "owner", "created_by",
    "created", "updated", "checkin_at", "progress", "result", "after", "notes", "thread",
}


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.state = Worklog()
        self.state.tasks[9] = Task(
            9, "Publish release", desc="Ship the release.", area="project-z", files="release.py",
            prio=3, status="done", owner="zeta@b", created_by="alpha@a", created=10.0,
            updated=40.0, checkin_at=25.0, progress="Built artifacts", result="Release published",
            after=[3, 1], notes=[(10.0, "alpha@a", "Started planning"), (40.0, "zeta@b", "Published")],
            via=999, passes={"alpha@a": "Other work"},
        )
        self.state.tasks[2] = Task(
            2, "Build <unsafe> view", desc="Render <script> as text.", area="project-a",
            files="view.py, view.css", prio=1, status="claimed", owner="alpha@a",
            created_by="zeta@b", created=5.0, updated=20.0, checkin_at=20.0,
            progress="Half complete", notes=[(20.0, "alpha@a", "[check-in] Half complete")],
        )
        self.state.tasks[5] = Task(
            5, "Write guide", desc="Document the API.", area="project-a", files="README.md",
            created_by="alpha@a", created=7.0, updated=7.0,
        )
        self.state.workers["zeta@b"] = Worker(
            "zeta@b", machine="b", bot=222, channel=333, desc="Release worker",
            session="secret-z", seen=40.0, areas=["zeta", "alpha"],
        )
        self.state.workers["alpha@a"] = Worker(
            "alpha@a", machine="a", bot=111, channel=444, desc="Build worker",
            session="secret-a", seen=20.0, areas=["build"],
        )
        self.state.leases["gpu"] = Lease("gpu", "zeta@b", "Benchmark", 30.0, 90.0)
        self.state.leases["cache"] = Lease("cache", "alpha@a", "Rebuild", 10.0, 70.0)
        self.state.history = [(float(i), "alpha@a", f"event-{i}") for i in range(HISTORY_LIMIT + 5)]
        self.state.messages = [
            Message(101, "human:dashboard", "alpha@a", 2, 30.0, "Is this <blocked>?"),
            Message(102, "alpha@a", "human:dashboard", 2, 31.0, "Still waiting for review."),
            Message(103, "alpha@a", "zeta@b", None, 32.0, "Unrelated mail"),
        ]

    def test_snapshot_contains_every_task_and_all_public_fields(self):
        data = snapshot(self.state, "machine-a", now=1234.5)
        self.assertEqual(data["generated_at"], 1234.5)
        self.assertEqual(data["machine"], "machine-a")
        self.assertEqual([task["num"] for task in data["tasks"]], [2, 5, 9])
        for task in data["tasks"]:
            self.assertEqual(set(task), TASK_FIELDS)
        build = data["tasks"][0]
        self.assertEqual(build["title"], "Build <unsafe> view")
        self.assertEqual(build["desc"], "Render <script> as text.")
        self.assertEqual(build["files"], "view.py, view.css")
        self.assertEqual(build["progress"], "Half complete")
        release = data["tasks"][2]
        self.assertEqual(release["result"], "Release published")
        self.assertEqual(release["after"], [1, 3])
        self.assertNotIn("via", release)
        self.assertNotIn("passes", release)

    def test_snapshot_preserves_all_notes_as_json_objects(self):
        data = snapshot(self.state, "a", now=50.0)
        release = data["tasks"][2]
        self.assertEqual(release["notes"], [
            {"at": 10.0, "by": "alpha@a", "text": "Started planning"},
            {"at": 40.0, "by": "zeta@b", "text": "Published"},
        ])
        build = data["tasks"][0]
        self.assertEqual(build["notes"], [
            {"at": 20.0, "by": "alpha@a", "text": "[check-in] Half complete"},
        ])

    def test_snapshot_groups_only_task_messages_into_threads(self):
        data = snapshot(self.state, "a", now=50.0)
        self.assertEqual(data["tasks"][0]["thread"], [
            {"id": 101, "sender": "human:dashboard", "to": "alpha@a", "at": 30.0,
             "text": "Is this <blocked>?"},
            {"id": 102, "sender": "alpha@a", "to": "human:dashboard", "at": 31.0,
             "text": "Still waiting for review."},
        ])
        self.assertEqual(data["tasks"][1]["thread"], [])
        self.assertEqual(data["tasks"][2]["thread"], [])

    def test_workers_and_leases_are_sorted_and_allowlisted(self):
        data = snapshot(self.state, "a", now=50.0)
        self.assertEqual([worker["name"] for worker in data["workers"]], ["alpha@a", "zeta@b"])
        for worker in data["workers"]:
            self.assertEqual(set(worker), {"name", "machine", "desc", "seen", "areas"})
            for private in ("session", "bot", "channel"):
                self.assertNotIn(private, worker)
        self.assertEqual(data["workers"][1]["areas"], ["alpha", "zeta"])
        encoded = json.dumps(data["workers"])
        self.assertNotIn("secret-a", encoded)
        self.assertNotIn("secret-z", encoded)
        self.assertEqual([lease["resource"] for lease in data["leases"]], ["cache", "gpu"])
        for lease in data["leases"]:
            self.assertEqual(set(lease), {"resource", "owner", "why", "since", "expires"})
        self.assertEqual(data["leases"][1]["why"], "Benchmark")

    def test_recent_history_is_bounded_chronological_and_json_safe(self):
        data = snapshot(self.state, "a", now=50.0)
        self.assertEqual(len(data["history"]), HISTORY_LIMIT)
        self.assertEqual(data["history"][0], {"at": 5.0, "by": "alpha@a", "text": "event-5"})
        self.assertEqual(data["history"][-1]["text"], f"event-{HISTORY_LIMIT + 4}")
        self.assertEqual(json.loads(json.dumps(data, allow_nan=False)), data)

    def test_default_time_and_snapshot_list_copies(self):
        with patch("harness.worklog_dashboard.time.time", return_value=777.0):
            data = snapshot(self.state, "a")
        self.assertEqual(data["generated_at"], 777.0)
        data["tasks"][2]["after"].append(99)
        data["workers"][0]["areas"].append("new")
        self.assertEqual(self.state.tasks[9].after, [3, 1])
        self.assertEqual(self.state.workers["alpha@a"].areas, ["build"])

    def test_empty_state_is_json_safe(self):
        self.assertEqual(snapshot(Worklog(), "none", now=0.0), {
            "generated_at": 0.0, "machine": "none", "tasks": [], "todo": [], "areas": {}, "workers": [], "leases": [], "history": [],
        })


class PageTests(unittest.TestCase):
    def setUp(self):
        self.html = page()

    def test_page_is_self_contained_and_responsive(self):
        self.assertTrue(self.html.startswith("<!doctype html>"))
        self.assertIn('<meta name="viewport"', self.html)
        self.assertIn("<style>", self.html)
        self.assertIn("<script>", self.html)
        self.assertIn("@media (max-width: 560px)", self.html)
        self.assertNotIn("<link", self.html)
        self.assertNotIn("<img", self.html)
        self.assertNotRegex(self.html, r"<script[^>]+src=")

    def test_page_has_filters_counts_refresh_and_auto_refresh(self):
        for element_id in (
            "search", "stats", "refresh", "auto-refresh", "areas", "workers",
            "leases", "history", "error", "no-matches",
        ):
            self.assertIn(f'id="{element_id}"', self.html)
        self.assertIn('type="search"', self.html)
        self.assertNotIn("<select", self.html)
        self.assertIn("const DEFAULT_STATUSES = ['open', 'blocked'];", self.html)
        self.assertIn("node('button', 'stat'", self.html)
        self.assertIn("card.setAttribute('aria-pressed'", self.html)
        self.assertIn("Auto-refresh every 10s", self.html)
        self.assertIn("window.setInterval(() => refresh(true), 10000)", self.html)
        self.assertIn("if (inFlight || (auto === true && typing())) return;", self.html)
        self.assertIn("fetch('state' + window.location.search", self.html)
        self.assertIn("if (!response.ok)", self.html)
        self.assertIn('role="alert"', self.html)

    def test_page_has_safe_threaded_replies_with_pending_and_feedback_states(self):
        self.assertIn("function threadFor(task)", self.html)
        self.assertIn("node('form', 'reply-form')", self.html)
        self.assertIn("fetch('reply' + window.location.search", self.html)
        self.assertIn("method: 'POST'", self.html)
        self.assertIn("JSON.stringify({task: num, text: text})", self.html)
        self.assertIn("replyPending.add(num)", self.html)
        self.assertIn("replyFeedback.set(num, {kind: 'success'", self.html)
        self.assertIn("replyFeedback.set(num, {kind: 'error'", self.html)
        self.assertIn("!task.owner || replyPending.has(task.num)", self.html)
        self.assertNotIn("innerHTML", self.html)

    def test_page_builds_collapsible_safe_task_views_and_empty_states(self):
        self.assertIn("node('details', 'area')", self.html)
        self.assertIn("node('details', 'task')", self.html)
        self.assertIn("latestStatus", self.html)
        self.assertIn("'Activity · '", self.html)
        self.assertIn("'Status report'", self.html)
        self.assertIn("richText(task.result, 'description')", self.html)
        self.assertIn("'Log · ' + kind", self.html)
        self.assertIn(".sort((a, b) => (Number(b.at) || 0) - (Number(a.at) || 0))", self.html)
        self.assertIn("section(task, 'information', 'Information', false, fields)", self.html)
        self.assertIn("node('details', 'section')", self.html)
        self.assertIn("Depends on", self.html)
        self.assertIn("No tasks in this worklog.", self.html)
        self.assertIn("No workers registered.", self.html)
        self.assertIn("No recent activity.", self.html)
        self.assertIn("element.textContent = String(text)", self.html)
        self.assertNotIn("innerHTML", self.html)


class TodoTests(unittest.TestCase):
    def setUp(self):
        self.state = Worklog()

    def add(self, num, result, status="blocked", prio=2, after=()):
        self.state.tasks[num] = Task(num, f"Task {num}", status=status, prio=prio, owner="a@b",
                                     result=result, after=list(after))

    def groups(self):
        return {group["key"]: group for group in todo(self.state)}

    def test_groups_blocked_tasks_by_action(self):
        self.add(1, "Code done; device test on the Pixel 10a needs the user")
        self.add(2, "Need SSH (key rejected). On host-a, in PowerShell run as Administrator, run this one line: Add-Content $f $k")
        self.add(3, "Needs Google client credentials from the user")
        self.add(4, "needs user review: options in desc")
        self.add(5, "waits for the user's explicit go-ahead before any PS5 input")
        self.add(6, "Need console awake and network-connected")
        self.add(7, "waits for a map large enough to need tiles (user: revisit then)")
        self.add(8, "Stopped the repair; no live input")
        self.add(9, "Done earlier", status="done")
        self.add(10, "Needs user review", status="open")
        groups = self.groups()
        self.assertEqual([g["key"] for g in todo(self.state)],
                         ["approve", "run", "device", "hardware", "accounts", "decide", "later", "other"])
        found = {key: [item["num"] for item in group["items"]] for key, group in groups.items()}
        self.assertEqual(found, {"approve": [5], "run": [2], "device": [1], "hardware": [6],
                                 "accounts": [3], "decide": [4], "later": [7], "other": [8]})

    def test_run_items_split_command_from_action(self):
        self.add(2, "Need SSH to host-a (key rejected). On host-a, as Administrator, run this one line: Add-Content $f $k")
        self.add(3, "Kernel module missing. Run: sudo bash /home/u/fix.sh")
        items = {item["num"]: item for item in self.groups()["run"]["items"]}
        self.assertEqual(items[2]["command"], "Add-Content $f $k")
        self.assertEqual(items[2]["action"], "Need SSH to host-a (key rejected)")
        self.assertEqual(items[3]["command"], "sudo bash /home/u/fix.sh")
        self.assertIn("run this one line", items[2]["reason"])

    def test_action_is_the_clause_that_says_what_is_needed(self):
        self.add(1, "Wiring done (c0af2ab); needs Apple credentials from the user. Steps in README")
        self.assertEqual(self.groups()["accounts"]["items"][0]["action"], "Needs Apple credentials from the user")

    def test_long_action_is_cut_to_one_line(self):
        self.add(1, "Needs user review of " + "x" * 300)
        action = self.groups()["decide"]["items"][0]["action"]
        self.assertLessEqual(len(action), 140)
        self.assertTrue(action.endswith("…"))

    def test_skips_tasks_that_wait_for_unfinished_tasks(self):
        self.add(1, "Code in progress", status="claimed")
        self.add(2, "human task: waits for #1 tests to pass; needs the user on the phone", after=[1])
        self.add(3, "needs user review", after=[4])
        self.add(4, "done", status="done")
        groups = self.groups()
        self.assertNotIn("device", groups)
        self.assertEqual([item["num"] for item in groups["decide"]["items"]], [3])

    def test_items_sort_by_priority_then_number_and_snapshot_has_todo(self):
        self.add(7, "needs user review", prio=3)
        self.add(9, "needs user review", prio=1)
        self.add(4, "needs user review", prio=3)
        self.assertEqual([i["num"] for i in self.groups()["decide"]["items"]], [9, 4, 7])
        self.assertEqual(snapshot(self.state, "m", now=1.0)["todo"], todo(self.state))

    def test_page_has_todo_section_with_task_links(self):
        html = page()
        self.assertIn('id="todo"', html)
        self.assertIn("To do for you", html)
        self.assertIn("link.href = '#task-' + item.num", html)
        self.assertIn("details.id = 'task-' + task.num", html)
        self.assertIn("if (item.command) li.append(copyBlock(item.command));", html)
        self.assertIn("hashchange", html)

    def test_page_has_drop_button_next_to_reply(self):
        html = page()
        self.assertIn("if (!['done', 'dropped'].includes(task.status)) actions.append(dropButton(task, status));", html)
        self.assertIn("fetch('drop' + window.location.search", html)
        self.assertIn("window.prompt(", html)
        self.assertIn("if (reason === null) return;", html)



class AnswerTests(unittest.TestCase):
    def test_one_tap_answers(self):
        from harness.worklog_dashboard import _answer
        self.assertEqual(_answer("run", "On host-a, run: sudo x"), "done")
        self.assertEqual(_answer("approve", "waits for the user's explicit go-ahead"), "ok")
        self.assertEqual(_answer("decide", "Needs user OK: push 15266e0 to GitHub"), "ok")
        self.assertEqual(_answer("decide", "needs user review: options in desc"), "")
        self.assertEqual(_answer("decide", "Waiting for you to pick dev or dev_hh"), "")
        self.assertEqual(_answer("device", "device test on the Pixel needs the user"), "")

    def test_todo_items_carry_the_answer(self):
        st = Worklog()
        st.tasks[1] = Task(1, "Push", status="blocked", owner="a@b", result="Needs user OK: push to main")
        st.tasks[2] = Task(2, "Key", status="blocked", owner="a@b", result="On host-a, run: echo k")
        answers = {i["num"]: i["answer"] for g in todo(st) for i in g["items"]}
        self.assertEqual(answers, {1: "ok", 2: "done"})

    def test_page_offers_send_ok_and_done(self):
        html = page()
        self.assertIn("label: 'Send OK', text: 'OK, go ahead.'", html)
        self.assertIn("label: 'Done', text: 'Done: I ran the command.", html)
        self.assertIn("if (quick && QUICK[quick.answer]) actions.append(quickButton(", html)
        self.assertIn("if (QUICK[item.answer]) {", html)


class AreaTests(unittest.TestCase):
    def test_area_links_and_notes_in_snapshot_and_wl(self):
        from harness.worklog import run_command
        st = Worklog()
        base = (int(1.8e12) - 1420070400000) << 22

        def apply(i, argv, who="a@b"):
            return st.apply(base + (i << 22), run_command(st, argv, who, set())[1])

        self.assertTrue(apply(1, ["area", "Placeframe", "--link", "https://x.ts.net/m", "--label", "Mapper"])[0])
        self.assertTrue(apply(2, ["area", "placeframe", "--note", "Restart with `mapper/restart.sh`"], "jo@x")[0])
        self.assertFalse(apply(3, ["area", "placeframe", "--link", "javascript:alert(1)"])[0])
        self.assertFalse(apply(4, ["area", "placeframe", "--unlink", "Nope"])[0])
        self.assertTrue(apply(5, ["area", "placeframe", "--link", "https://x.ts.net/v", "--label", "Viewer"])[0])
        self.assertTrue(apply(6, ["area", "placeframe", "--unlink", "Viewer"])[0])
        data = snapshot(st, "m", now=1.0)["areas"]
        self.assertEqual(list(data), ["placeframe"])
        self.assertEqual(data["placeframe"]["links"], {"Mapper": "https://x.ts.net/m"})
        self.assertEqual(data["placeframe"]["notes"][0]["by"], "jo@x")
        shown = run_command(st, ["area", "placeframe"], "a@b", set())[0]
        self.assertIn("Mapper: https://x.ts.net/m", shown)
        self.assertIn("Restart with", shown)

    def test_page_has_area_details_with_safe_links(self):
        html = page()
        self.assertIn("if (area) list.append(areaDetails(area, info));", html)
        self.assertIn("return /^https?:\\/\\//i.test(String(url || ''))", html)
        self.assertIn("link.rel = 'noopener noreferrer';", html)
        self.assertIn("fetch('area' + window.location.search", html)


if __name__ == "__main__":
    unittest.main()

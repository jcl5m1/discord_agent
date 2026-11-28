"""Shared worklog: tasks, claims, check-ins, leases and messages for every agent.

All harness bots on a Discord server share one worklog. Each change is an
*event* posted to the #worklog channel (see worklog_service.py); every bot
replays the same events in the same order (Discord message ids are globally
ordered), so all machines agree on who holds what. This module is the part
with no Discord in it: the event rules (Worklog.apply) and the `wl` commands
agents run (run_command), which read local state and return events to post.

Rules worth knowing:
- a claim held by another worker is refused unless it has lapsed: no check-in
  for CHECKIN_EVERY seconds (the owning harness also releases it then);
- check-ins carry a progress line of at most CHECKIN_WORDS words;
- a lease held by someone else is refused until it expires.
"""

from __future__ import annotations

import argparse
import json
import shlex
import time
from dataclasses import dataclass, field

CHECKIN_EVERY = 30 * 60  # seconds a claim stays valid without a check-in
CHECKIN_WORDS = 10
GONE_AFTER = 3 * 86400  # status hides an agent not seen this long (and holding no claim)
GONE_AFTER_SUB = 2 * 3600  # ... and a subagent (`<agent>/sub-N`) after this long
GONE_AFTER_TERMINAL = 86400  # ... and a worker no harness bot runs (a terminal session) after this long
STALE_AFTER = CHECKIN_EVERY  # a worker not seen for this long (or holding a lapsed claim) is stale
DISCORD_EPOCH_MS = 1420070400000
OPS = {"register", "add", "claim", "checkin", "note", "done", "block", "release",
       "reopen", "drop", "lease", "unlease", "send", "depends", "pass", "update", "updated"}


def event_time(event_id: int) -> float:
    """Unix time of a Discord message id (a snowflake)."""
    return ((event_id >> 22) + DISCORD_EPOCH_MS) / 1000


def fmt_time(t: float | None) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(t)) if t else "-"


def words(text: str) -> int:
    return len(text.split())


@dataclass
class Worker:
    name: str
    machine: str = ""
    bot: int = 0  # Discord user id of the harness bot running this worker (0: none)
    channel: int = 0  # its Discord channel for harness notifications
    desc: str = ""
    session: str = ""
    seen: float = 0.0
    areas: list[str] = field(default_factory=list)  # extra task areas it takes work in (`wl areas`)


@dataclass
class Task:
    num: int
    title: str
    desc: str = ""
    area: str = ""
    files: str = ""
    prio: int = 2
    status: str = "open"  # open | claimed | blocked | done | dropped
    owner: str = ""
    created_by: str = ""
    created: float = 0.0
    updated: float = 0.0
    checkin_at: float = 0.0  # last claim or check-in by the owner
    progress: str = ""  # the owner's latest check-in line
    via: int = 0  # the bot that posted the latest claim or check-in (it releases a lapsed one)
    result: str = ""
    notes: list[tuple[float, str, str]] = field(default_factory=list)
    after: list[int] = field(default_factory=list)  # tasks that must be done (or dropped) before this one
    passes: dict[str, str] = field(default_factory=dict)  # worker -> why it didn't take this task

    def lapsed(self, now: float) -> bool:
        return self.status == "claimed" and now - self.checkin_at > CHECKIN_EVERY


@dataclass
class Lease:
    resource: str
    owner: str
    why: str
    since: float
    expires: float


@dataclass
class Message:
    id: int  # the event id
    sender: str
    to: str
    task: int | None
    at: float
    text: str


class Worklog:
    """State rebuilt by applying events in id order; nothing else changes it."""

    def __init__(self) -> None:
        self.workers: dict[str, Worker] = {}
        self.tasks: dict[int, Task] = {}
        self.leases: dict[str, Lease] = {}
        self.messages: list[Message] = []
        self.history: list[tuple[float, str, str]] = []  # (time, who, what)
        self.results: dict[int, tuple[bool, str]] = {}  # event id -> outcome
        # `/update all` requests, by event id: who asked, when, where to report, and
        # each machine's result ({machine: {"ok", "text", "old", "new", "at"}}).
        self.updates: dict[int, dict] = {}
        self.last_id = 0
        self._event_id = 0

    # ------------------------------------------------------------------ rules
    def apply(self, event_id: int, ev: dict) -> tuple[bool, str]:
        """Apply one event; returns (accepted, outcome text) and records it."""
        t = event_time(event_id)
        self.last_id = max(self.last_id, event_id)
        by = str(ev.get("by", ""))
        op = ev.get("op")
        if op not in OPS or not by:
            return self._done(event_id, False, f"unknown event {op!r}")
        w = self.workers.setdefault(by, Worker(by))
        w.seen = max(w.seen, t)
        self._event_id = event_id
        ok, text = getattr(self, f"_op_{op}")(by, t, ev)
        if ok:
            self.history.append((t, by, text))
        return self._done(event_id, ok, text)

    def _done(self, event_id: int, ok: bool, text: str) -> tuple[bool, str]:
        self.results[event_id] = (ok, text)
        return ok, text

    def _task(self, ev: dict) -> Task | None:
        try:
            return self.tasks.get(int(ev.get("task")))
        except (TypeError, ValueError):
            return None

    def _op_register(self, by, t, ev):
        w = self.workers[by]
        for key in ("machine", "desc", "session"):
            if ev.get(key):
                setattr(w, key, str(ev[key]))
        for key in ("bot", "channel"):
            if ev.get(key):
                setattr(w, key, int(ev[key]))
        if isinstance(ev.get("areas"), list):
            w.areas = [norm_area(a) for a in ev["areas"] if norm_area(a)]
            return True, f"{by} works in: " + (", ".join(w.areas) or "(its folder and name only)")
        return True, "registered"

    def _op_add(self, by, t, ev):
        num = len(self.tasks) + 1
        self.tasks[num] = Task(
            num, str(ev.get("title", "")).strip() or "(untitled)", desc=str(ev.get("desc", "")),
            area=str(ev.get("area", "")), files=str(ev.get("files", "")),
            prio=int(ev.get("prio", 2) or 2), created_by=by, created=t, updated=t,
            after=_nums(ev.get("after")),
        )
        if ev.get("owner"):
            task = self.tasks[num]
            task.owner, task.status, task.checkin_at = str(ev["owner"]), "claimed", t
        return True, f"added #{num} {self.tasks[num].title}"

    def _op_claim(self, by, t, ev):
        task = self._task(ev)
        if task is None:
            return False, f"no task #{ev.get('task')}"
        if task.status in ("done", "dropped"):
            return False, f"#{task.num} is {task.status}"
        if task.owner and task.owner != by and task.status == "claimed" and not task.lapsed(t):
            return False, (f"#{task.num} is held by {task.owner} (last check-in "
                           f"{fmt_time(task.checkin_at)}). Message them instead.")
        took = task.owner and task.owner != by
        if took:
            self.messages.append(Message(-len(self.messages) - 1, by, task.owner, task.num, t,
                                         f"I took over #{task.num} '{task.title}': your claim had lapsed."))
        task.owner, task.status, task.checkin_at, task.updated = by, "claimed", t, t
        task.progress = str(ev.get("text", "")).strip()
        task.via = int(ev.get("via") or 0)
        return True, f"claimed #{task.num} {task.title}"

    def _op_checkin(self, by, t, ev):
        task = self._task(ev)
        text = str(ev.get("text", "")).strip()
        if task is None:
            return False, f"no task #{ev.get('task')}"
        if task.owner != by or task.status != "claimed":
            return False, f"#{task.num} isn't claimed by {by}"
        if not text or words(text) > CHECKIN_WORDS:
            return False, f"a check-in needs 1-{CHECKIN_WORDS} words of progress"
        task.checkin_at, task.updated, task.progress = t, t, text
        task.via = int(ev.get("via") or task.via)
        task.notes.append((t, by, f"[check-in] {text}"))
        return True, f"#{task.num} check-in: {text}"

    def _op_note(self, by, t, ev):
        task = self._task(ev)
        if task is None:
            return False, f"no task #{ev.get('task')}"
        task.notes.append((t, by, str(ev.get("text", ""))))
        task.updated = t
        return True, f"noted #{task.num}"

    def _status(self, by, t, ev, status, verb):
        task = self._task(ev)
        if task is None:
            return False, f"no task #{ev.get('task')}"
        allowed = {task.owner, task.created_by, ""} | ({by} if ev.get("harness") else set())
        if task.owner and by not in allowed and not ev.get("force"):
            return False, f"#{task.num} is held by {task.owner}"
        text = str(ev.get("text", "")).strip()
        task.status, task.updated = status, t
        task.owner = "" if status == "open" else (task.owner or by)
        if text:
            task.notes.append((t, by, f"[{verb}] {text}"))
            if status in ("done", "blocked"):
                task.result = text
        if task.created_by and task.created_by != by and status in ("done", "blocked"):
            self.messages.append(Message(-len(self.messages) - 1, by, task.created_by, task.num, t,
                                         f"#{task.num} '{task.title}' {verb}: {text}"))
        return True, f"#{task.num} {verb}"

    def _op_done(self, by, t, ev):
        return self._status(by, t, ev, "done", "done")

    def _op_block(self, by, t, ev):
        return self._status(by, t, ev, "blocked", "blocked")

    def _op_release(self, by, t, ev):
        return self._status(by, t, ev, "open", "released")

    def _op_drop(self, by, t, ev):
        return self._status(by, t, ev, "dropped", "dropped")

    def _op_reopen(self, by, t, ev):
        task = self._task(ev)
        if task is None:
            return False, f"no task #{ev.get('task')}"
        task.status, task.owner, task.updated = "open", "", t
        return True, f"#{task.num} reopened"

    def _op_lease(self, by, t, ev):
        res = str(ev.get("res", "")).strip()
        held = self.leases.get(res)
        if not res:
            return False, "lease needs a resource name"
        if held and held.owner != by and held.expires > t:
            return False, f"{res} is leased by {held.owner} until {fmt_time(held.expires)} ({held.why})"
        ttl = max(60, int(ev.get("ttl", 3600) or 3600))
        self.leases[res] = Lease(res, by, str(ev.get("why", "")), t, t + ttl)
        return True, f"leased {res} until {fmt_time(t + ttl)}"

    def _op_unlease(self, by, t, ev):
        res = str(ev.get("res", "")).strip()
        held = self.leases.get(res)
        if held and held.owner != by and not ev.get("force"):
            return False, f"{res} is leased by {held.owner}"
        self.leases.pop(res, None)
        return True, f"released {res}"

    def _op_depends(self, by, t, ev):
        task = self._task(ev)
        if task is None:
            return False, f"no task #{ev.get('task')}"
        after = [n for n in _nums(ev.get("after")) if n != task.num]
        missing = [n for n in after if n not in self.tasks]
        if missing:
            return False, f"no task #{missing[0]}"
        task.after, task.updated = after, t
        task.notes.append((t, by, "[depends] " + (", ".join(f"#{n}" for n in after) or "nothing")))
        return True, f"#{task.num} waits on " + (", ".join(f"#{n}" for n in after) or "nothing")

    def _op_pass(self, by, t, ev):
        task = self._task(ev)
        text = str(ev.get("text", "")).strip()
        if task is None:
            return False, f"no task #{ev.get('task')}"
        if not text:
            return False, "say why you don't take it"
        task.passes[by] = text
        task.notes.append((t, by, f"[pass] {text}"))
        return True, f"passed on #{task.num}"

    def waiting_on(self, task: Task) -> list[int]:
        """The tasks this one waits for that aren't done or dropped yet."""
        return [n for n in task.after if n in self.tasks and self.tasks[n].status not in ("done", "dropped")]

    def ready(self, worker: str = "") -> list[Task]:
        """Open tasks nobody holds and nothing blocks, best first; without those `worker` passed on."""
        base = worker.split("/")[0]
        return sorted((t for t in self.tasks.values()
                       if t.status == "open" and not self.waiting_on(t)
                       and not (worker and (worker in t.passes or base in t.passes))),
                      key=lambda t: (t.prio, t.num))

    def _op_send(self, by, t, ev):
        to = str(ev.get("to", "")).strip()
        text = str(ev.get("text", "")).strip()
        if not text:
            return False, "empty message"
        if to != "all" and to not in self.workers:
            return False, f"no worker {to} (see `wl status`)"
        task = ev.get("task")
        self.messages.append(Message(self._event_id, by, to, int(task) if task else None, t, text))
        return True, f"sent to {to}"

    def _op_update(self, by, t, ev):
        self.updates[self._event_id] = {
            "by": by, "at": t, "force": bool(ev.get("force")),
            "machine": str(ev.get("machine", "")), "channel": int(ev.get("channel") or 0),
            "results": {},
        }
        return True, f"{by} asked every harness to update" + (" (force)" if ev.get("force") else "")

    def _op_updated(self, by, t, ev):
        try:
            req = int(ev.get("req"))
        except (TypeError, ValueError):
            return False, "no update request id"
        machine = str(ev.get("machine", "")).strip()
        if req not in self.updates or not machine:
            return False, f"no update request {req}"
        text = str(ev.get("text", "")).strip()
        self.updates[req]["results"][machine] = {
            "ok": bool(ev.get("ok")), "text": text, "old": str(ev.get("old", "")),
            "new": str(ev.get("new", "")), "at": t,
        }
        return True, f"{machine} update: {text}"

    # ------------------------------------------------------------------ reads
    def inbox(self, worker: str, read: set[int]) -> list[Message]:
        return [m for m in self.messages
                if (m.to == worker or (m.to == "all" and m.sender != worker)) and m.id not in read]

    def is_stale(self, worker: str, now: float) -> bool:
        """Not seen for CHECKIN_EVERY, or holding a claim it hasn't checked in on."""
        w = self.workers.get(worker)
        if w is None or now - w.seen >= STALE_AFTER:
            return True
        return any(t.lapsed(now) for t in self.owned_by(worker))

    def is_gone(self, worker: str, now: float, live: set[str] | None = None, machine: str = "") -> bool:
        """A worker that no longer exists, for the status list (never one that holds a claim).

        live (with machine): the harness agents that exist on `machine` now. A worker of that machine
        that isn't one of them is gone (a removed instance, or a sender that isn't an agent), and so
        are its subagents. Otherwise: unseen for GONE_AFTER (an agent a bot runs), GONE_AFTER_TERMINAL
        (no bot: a terminal session) or GONE_AFTER_SUB (a subagent).
        """
        w = self.workers.get(worker)
        if w is None or self.owned_by(worker):
            return False
        base = worker.split("/")[0]
        if live is not None and machine and base.endswith("@" + machine) and base not in live:
            return True
        if "/" in worker:
            return now - w.seen >= GONE_AFTER_SUB or (base in self.workers and self.is_gone(base, now, live, machine))
        parent = self.workers.get(base)
        return now - w.seen >= (GONE_AFTER if parent and parent.bot else GONE_AFTER_TERMINAL)

    def owned_by(self, worker: str) -> list[Task]:
        return [t for t in self.tasks.values() if t.owner == worker and t.status == "claimed"]


# ---------------------------------------------------------------- commands

class CommandError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):  # report instead of exiting the harness
        raise CommandError(f"{message}\n{self.format_usage()}")


def norm_area(area: str) -> str:
    return "-".join("".join(c if c.isalnum() else " " for c in str(area).lower()).split())


def area_keys(worker: Worker, aliases: dict[str, str] | None = None) -> set[str]:
    """The task areas a worker takes work in: its folder's name, its name, and `wl areas`."""
    keys = {norm_area(worker.name.split("@")[0].split("/")[0])}
    if " in " in worker.desc:
        folder = worker.desc.split(" in ", 1)[1].strip().rstrip("/").rsplit("/", 1)[-1]
        keys.add(norm_area(folder))
    keys |= set(worker.areas)
    alias = {norm_area(k): norm_area(v) for k, v in (aliases or {}).items()}
    keys |= {alias[k] for k in keys if k in alias}
    return {k for k in keys if k}


def area_matches(area: str, keys: set[str]) -> bool:
    """A task area matches a key that equals it or extends it ("placeframe" ~ "placeframe-insta360")."""
    a = norm_area(area)
    return bool(a) and any(a == k or k.startswith(a + "-") or a.startswith(k + "-") for k in keys)


def _nums(value) -> list[int]:
    """Task numbers from a list or a text like "42, #43"."""
    if isinstance(value, (list, tuple)):
        items = value
    else:
        items = str(value or "").replace(",", " ").split()
    out = []
    for x in items:
        try:
            out.append(int(str(x).lstrip("#")))
        except ValueError:
            continue
    return out


def _parser() -> _Parser:
    p = _Parser(prog="wl", add_help=False)
    p.add_argument("--as", dest="as_")
    s = p.add_subparsers(dest="cmd", required=True, parser_class=_Parser)

    def sub(name, *args):
        sp = s.add_parser(name, add_help=False)
        for a in args:
            a(sp)
        return sp

    num = lambda sp: sp.add_argument("task", type=int)  # noqa: E731
    text = lambda sp: sp.add_argument("text", nargs="?", default="")  # noqa: E731
    force = lambda sp: sp.add_argument("--force", action="store_true")  # noqa: E731
    sub("register", lambda sp: sp.add_argument("--desc", default=""),
        lambda sp: sp.add_argument("--session", default=""))
    sub("add", lambda sp: sp.add_argument("title"), lambda sp: sp.add_argument("--desc", default=""),
        lambda sp: sp.add_argument("--area", default=""), lambda sp: sp.add_argument("--files", default=""),
        lambda sp: sp.add_argument("--prio", type=int, default=2),
        lambda sp: sp.add_argument("--owner", default=""), lambda sp: sp.add_argument("--after", default=""))
    sub("depends", num, lambda sp: sp.add_argument("--on", default=""))
    sub("pass", num, lambda sp: sp.add_argument("text"))
    sub("next")
    sub("areas", lambda sp: sp.add_argument("list", nargs="?"))
    sub("claim", num, text)
    for name in ("checkin", "ci"):
        sub(name, num, lambda sp: sp.add_argument("text"))
    sub("note", num, lambda sp: sp.add_argument("text"))
    for name in ("done", "block", "release", "drop"):
        sub(name, num, text, force)
    sub("reopen", num)
    sub("lease", lambda sp: sp.add_argument("res"), lambda sp: sp.add_argument("--ttl", type=int, default=3600),
        lambda sp: sp.add_argument("--why", default=""))
    sub("unlease", lambda sp: sp.add_argument("res"), force)
    sub("send", lambda sp: sp.add_argument("to"), lambda sp: sp.add_argument("text"),
        lambda sp: sp.add_argument("--task", type=int))
    sub("inbox", lambda sp: sp.add_argument("--peek", action="store_true"))
    sub("wait", lambda sp: sp.add_argument("--timeout", type=float, default=0),
        lambda sp: sp.add_argument("--poll", type=float, default=0))
    sub("list", lambda sp: sp.add_argument("--all", action="store_true"))
    sub("show", num)
    sub("status", lambda sp: sp.add_argument("--all", action="store_true"))
    sub("workers", lambda sp: sp.add_argument("--all", action="store_true"))
    sub("log", lambda sp: sp.add_argument("-n", type=int, default=30))
    sub("help")
    return p


HELP = f"""wl — the shared worklog for every agent on this Discord server (all machines).
  wl status                         workers, open tasks, leases, your unread messages
  wl list [--all] | show N | log [-n 30] | workers [--all]
  wl add "title" [--desc ..] [--area ..] [--files a.py,b.py] [--prio 1-3] [--after 42,43]
  wl next                           the best open task in your areas
  wl areas [a,b,c]                  show or set the extra task areas you take work in
  wl pass N "why you don't take it"  |  wl depends N --on 42,43  (N waits until they are done)
  wl claim N ["what you're starting, ≤{CHECKIN_WORDS} words"]
  wl checkin N "progress, ≤{CHECKIN_WORDS} words"     at least every {CHECKIN_EVERY // 60} min while you hold N
  wl note N "longer progress, commits, findings"
  wl done N "result" | block N "what you need" | release N | drop N | reopen N
  wl lease NAME --ttl 3600 --why ".."  |  wl unlease NAME
  wl send WORKER|all "text" [--task N]  |  wl inbox [--peek]
Add --as NAME to act as another worker (e.g. a subagent: --as <you>/sub-1)."""


def _task_line(t: Task, now: float) -> str:
    owner = t.owner or ""
    if t.status == "claimed":
        ago = int((now - t.checkin_at) / 60)
        owner += f" ({'LAPSED' if t.lapsed(now) else f'{ago}m ago'})"
    progress = f" — {t.progress}" if t.progress and t.status == "claimed" else ""
    if t.after and t.status == "open":
        progress += " (after " + ", ".join(f"#{n}" for n in t.after) + ")"
    area = f"[{t.area}] " if t.area else ""
    return f"#{t.num:<3} p{t.prio} {t.status:<8} {owner:<34} {area}{t.title}{progress}"


def run_command(state: Worklog, argv: list[str] | str, worker: str, read: set[int],
                now: float | None = None, live: set[str] | None = None,
                machine: str = "") -> tuple[str, dict | None, list[int]]:
    """Run a `wl` command for `worker`.

    Returns (text to print, event to post or None, message ids now read). For a
    command that changes state, the caller posts the event and reports the
    outcome Worklog.apply records for it.
    """
    now = time.time() if now is None else now
    argv = shlex.split(argv) if isinstance(argv, str) else list(argv)
    # --as is accepted anywhere on the line, not just before the command.
    as_ = None
    for i, arg in enumerate(argv):
        if arg == "--as" and i + 1 < len(argv):
            as_ = argv[i + 1]
            argv = argv[:i] + argv[i + 2:]
            break
        if arg.startswith("--as="):
            as_, argv = arg[5:], argv[:i] + argv[i + 1:]
            break
    if not argv or argv[0] in ("help", "-h", "--help"):
        return HELP, None, []
    a = _parser().parse_args(argv)
    who = as_ or a.as_ or worker
    if not who and a.cmd not in ("status", "list", "show", "log", "workers"):
        raise CommandError("who are you? pass --as NAME (WORKLOG_WORKER isn't set)")
    cmd = "checkin" if a.cmd == "ci" else a.cmd

    if cmd in ("register",):
        return "", {"op": "register", "by": who, "desc": a.desc, "session": a.session}, []
    if cmd == "add":
        return "", {"op": "add", "by": who, "title": a.title, "desc": a.desc, "area": a.area,
                    "files": a.files, "prio": a.prio, "owner": a.owner, "after": _nums(a.after)}, []
    if cmd == "depends":
        return "", {"op": "depends", "by": who, "task": a.task, "after": _nums(a.on)}, []
    if cmd == "pass":
        return "", {"op": "pass", "by": who, "task": a.task, "text": a.text}, []
    if cmd == "areas":
        if a.list is None:
            w = state.workers.get(who)
            from . import config
            keys = sorted(area_keys(w, config.WORKLOG_AREA_ALIASES)) if w else []
            return f"{who} takes tasks in: " + (", ".join(keys) or "(none)"), None, []
        return "", {"op": "register", "by": who, "areas": [x for x in a.list.replace(",", " ").split()]}, []
    if cmd == "next":
        w = state.workers.get(who.split("/")[0]) or state.workers.get(who)
        from . import config  # lazy: this module is otherwise free of harness settings
        keys = area_keys(w, config.WORKLOG_AREA_ALIASES) if w else set()
        ready = [t for t in state.ready(who) if area_matches(t.area, keys)]
        if not ready:
            return "(no open task you can take now)", None, []
        t = ready[0]
        return (_task_line(t, now) + (f"\n  {t.desc}" if t.desc else "")
                + f"\nClaim it: wl claim {t.num} \"<what you start>\"   Or say why not: "
                  f"wl pass {t.num} \"<why>\"", None, [])
    if cmd == "claim":
        if a.text and words(a.text) > CHECKIN_WORDS:
            raise CommandError(f"keep the claim's status to {CHECKIN_WORDS} words")
        return "", {"op": "claim", "by": who, "task": a.task, "text": a.text}, []
    if cmd == "checkin":
        if not a.text.strip() or words(a.text) > CHECKIN_WORDS:
            raise CommandError(f"a check-in is 1-{CHECKIN_WORDS} words of progress "
                               f"(got {words(a.text)}); use `wl note` for detail")
        return "", {"op": "checkin", "by": who, "task": a.task, "text": a.text}, []
    if cmd == "note":
        return "", {"op": "note", "by": who, "task": a.task, "text": a.text}, []
    if cmd in ("done", "block", "release", "drop"):
        return "", {"op": cmd, "by": who, "task": a.task, "text": a.text, "force": a.force}, []
    if cmd == "reopen":
        return "", {"op": "reopen", "by": who, "task": a.task}, []
    if cmd == "lease":
        return "", {"op": "lease", "by": who, "res": a.res, "ttl": a.ttl, "why": a.why}, []
    if cmd == "unlease":
        return "", {"op": "unlease", "by": who, "res": a.res, "force": a.force}, []
    if cmd == "send":
        return "", {"op": "send", "by": who, "to": a.to, "text": a.text, "task": a.task}, []

    # read-only commands
    if cmd in ("inbox", "wait"):
        msgs = state.inbox(who, read)
        lines = [f"[{fmt_time(m.at)}] {m.sender}{f' re #{m.task}' if m.task else ''}: {m.text}"
                 for m in msgs]
        note = ("" if cmd == "inbox" else
                "\n(no need to wait: the harness delivers worklog messages to you as they arrive)")
        mark = [] if cmd == "inbox" and a.peek else [m.id for m in msgs]
        return ("\n".join(lines) or "(no unread messages)") + note, None, mark
    if cmd == "list":
        shown = [t for t in sorted(state.tasks.values(), key=lambda t: (t.status == "done", t.prio, t.num))
                 if a.all or t.status not in ("done", "dropped")]
        return "\n".join(_task_line(t, now) for t in shown) or "(no tasks)", None, []
    if cmd == "show":
        t = state.tasks.get(a.task)
        if t is None:
            raise CommandError(f"no task #{a.task}")
        rows = [("title", t.title), ("status", t.status), ("owner", t.owner),
                ("progress", t.progress if t.status == "claimed" else ""), ("prio", t.prio),
                ("area", t.area), ("files", t.files), ("created_by", t.created_by),
                ("desc", t.desc), ("result", t.result),
                ("after", ", ".join(f"#{n}" for n in t.after)),
                ("waiting on", ", ".join(f"#{n}" for n in state.waiting_on(t)))]
        out = [f"{k:>10}: {v}" for k, v in rows if v not in ("", None)]
        out.append(f"{'created':>10}: {fmt_time(t.created)}   updated: {fmt_time(t.updated)}"
                   + (f"   last check-in: {fmt_time(t.checkin_at)}" if t.status == "claimed" else ""))
        out += [f"  [{fmt_time(at)}] {who_}: {text}" for at, who_, text in t.notes]
        return "\n".join(out), None, []
    if cmd in ("status", "workers"):
        out = ["Workers:"]
        shown = [w for w in state.workers.values() if a.all or not state.is_gone(w.name, now, live, machine)]
        hidden = len(state.workers) - len(shown)
        for w in sorted(shown, key=lambda w: -w.seen):
            st = "stale" if state.is_stale(w.name, now) else "active"
            where = f" on {w.machine}" if w.machine else ""
            out.append(f"  {w.name:<34} {st:<6} seen {fmt_time(w.seen)}{where}  {w.desc}")
        if hidden:
            out.append(f"  ({hidden} gone: no harness instance on {machine or 'this machine'}, or unseen "
                       f"{GONE_AFTER // 86400} days (terminal sessions {GONE_AFTER_TERMINAL // 3600} h, subagents "
                       f"{GONE_AFTER_SUB // 3600} h); none hold claims; --all shows them)")
        if cmd == "workers":
            return "\n".join(out), None, []
        out.append("Leases:")
        live = [l for l in state.leases.values() if l.expires > now]
        out += [f"  {l.resource:<14} {l.owner} until {fmt_time(l.expires)}  {l.why}" for l in live] or ["  (none)"]
        out.append("Open tasks:")
        open_ = [t for t in sorted(state.tasks.values(), key=lambda t: (t.prio, t.num))
                 if t.status not in ("done", "dropped")]
        out += [_task_line(t, now) for t in open_] or ["  (none)"]
        if who:
            out.append(f"Unread for {who}: {len(state.inbox(who, read))}")
        return "\n".join(out), None, []
    if cmd == "log":
        return "\n".join(f"[{fmt_time(at)}] {who_}: {what}" for at, who_, what in state.history[-a.n:]) \
            or "(empty)", None, []
    return HELP, None, []


# --------------------------------------------------------- event encoding
# Each event is one Discord message: a readable line, then the event as JSON
# on a small "-#" line. Backticks are escaped so the JSON can sit in `code`.
MARKER = "-# wl `"


def encode(ev: dict, summary: str) -> str:
    payload = json.dumps(ev, separators=(",", ":"), ensure_ascii=False).replace("`", "\\u0060")
    summary = summary.replace("\n", " ")[: 1900 - min(len(payload), 1700)]
    return f"{summary}\n{MARKER}{payload}`"


def decode(content: str) -> dict | None:
    for line in reversed(content.splitlines()):
        if line.startswith(MARKER) and line.endswith("`"):
            try:
                ev = json.loads(line[len(MARKER):-1])
            except ValueError:
                return None
            return ev if isinstance(ev, dict) else None
    return None


def summary(ev: dict) -> str:
    """The readable line shown in #worklog for an event."""
    op, by, num = ev.get("op"), ev.get("by"), ev.get("task")
    text = str(ev.get("text", "")).strip()
    return {
        "register": f"👋 {by} registered" + (f" on {ev.get('machine')}" if ev.get("machine") else ""),
        "add": f"➕ {by} added: {ev.get('title', '')}",
        "claim": f"🔒 {by} claims #{num}" + (f": {text}" if text else ""),
        "checkin": f"📋 #{num} · {by}: {text}",
        "note": f"📝 {by} on #{num}: {text[:300]}",
        "done": f"✅ {by} finished #{num}" + (f": {text[:300]}" if text else ""),
        "block": f"⛔ {by} blocked #{num}" + (f": {text[:300]}" if text else ""),
        "release": f"🔓 {by} released #{num}" + (f": {text[:300]}" if text else ""),
        "drop": f"🗑️ {by} dropped #{num}",
        "reopen": f"♻️ {by} reopened #{num}",
        "depends": f"🔗 #{num} waits on " + (", ".join(f"#{n}" for n in _nums(ev.get("after"))) or "nothing"),
        "pass": f"↪️ {by} passes on #{num}: {text[:300]}",
        "lease": f"🔑 {by} leases {ev.get('res')} ({ev.get('ttl')}s): {ev.get('why', '')}",
        "unlease": f"🔓 {by} released lease {ev.get('res')}",
        "update": f"⬇️ {by} asks every harness to update" + (" (force)" if ev.get("force") else ""),
        "updated": ("✅" if ev.get("ok") else "❌") + f" {ev.get('machine')} update: {text[:300]}",
        "send": f"✉️ {by} → {ev.get('to')}" + (f" re #{ev['task']}" if ev.get("task") else "")
                + f": {text[:300]}",
    }.get(op, f"{op} by {by}")

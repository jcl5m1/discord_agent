"""The worklog's Discord transport and the harness's part in it.

- #worklog channel: every change is one message there (worklog.encode). Each
  bot keeps the events in data/worklog.db and rebuilds the state by applying
  them in message-id order, which is the same on every machine. An event that
  arrives out of order (an earlier id after a later one) triggers a full replay.
- Local API: agents' `wl` command (bin/wl) posts its arguments to this bot on
  127.0.0.1, with a token from data/worklog_token, so agents never hold the bot
  token. Changes are posted to #worklog and confirmed after SETTLE seconds, so
  a competing event from another machine has time to arrive first.
- Harness duties, for the workers this bot runs: register them, post their
  check-ins to their own Discord channel, deliver messages sent to them (into
  the running turn, or as a new turn), remind them to check in, and release a
  claim that has gone CHECKIN_EVERY without a check-in.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import socket
import sqlite3
import time
from collections import deque
from typing import Awaitable, Callable

import discord
from aiohttp import web

from . import config
from .worklog import (CHECKIN_EVERY, CommandError, Worklog, area_keys, area_matches, decode, encode, fmt_time,
                      run_command, summary)

log = logging.getLogger("discord_agent.worklog")

SETTLE = 2.5  # seconds after posting before a change is confirmed
REMIND_AT = CHECKIN_EVERY - 5 * 60  # remind this long after the last check-in
WAKE_LIMIT = 12  # deliveries that start a turn, per worker per hour
OFFER_AFTER = 10 * 60  # an agent idle this long with no claim is offered the best open task
OFFER_WAIT = 30 * 60  # a task offered to one agent isn't offered to another for this long
OFFER_GAP = 60 * 60  # at most one offer per agent per this long
OFFER_NEW_AFTER = 120  # a task is offered only this long after it was added (time to add its dependencies)


class WorklogService:
    def __init__(
        self,
        client: discord.Client,
        deliver: Callable[[str, str, str], Awaitable[None]],
        notify: Callable[[str, str], Awaitable[None]],
        idle_for: Callable[[str], float | None] | None = None,
    ):
        """deliver(worker, sender, text) hands a message to that worker's agent;
        notify(worker, text) posts a line in that worker's Discord channel;
        idle_for(worker) is how long that worker's agent has been idle, in seconds
        (None: busy, stopped, or not one to offer work to)."""
        self.idle_for = idle_for
        self._offers: dict[int, tuple[str, float]] = {}  # task -> (worker, when) it was last offered
        self._offered: dict[str, float] = {}  # worker -> when it was last offered a task
        self.client = client
        self.deliver = deliver
        self.notify = notify
        self.machine = socket.gethostname().split(".")[0]
        self.state = Worklog()
        self.channel: discord.TextChannel | None = None
        self.ready = asyncio.Event()
        self.live_from = 0  # side effects only for events newer than the startup sync
        self._db = self._open_db()
        self._post_lock = asyncio.Lock()
        self._wakes: dict[str, deque] = {}
        self._reminded: set[tuple[int, float]] = set()
        self.token = self._load_token()
        self.url = f"http://127.0.0.1:{config.WORKLOG_PORT}"

    # ------------------------------------------------------------- storage
    def _open_db(self) -> sqlite3.Connection:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(config.DATA_DIR / "worklog.db")
        db.executescript("""
            create table if not exists events(id integer primary key, json text not null);
            create table if not exists reads(worker text, msg integer, primary key(worker, msg));
        """)
        return db

    def _load_token(self) -> str:
        path = config.DATA_DIR / "worklog_token"
        if path.exists():
            return path.read_text().strip()
        token = secrets.token_urlsafe(24)
        path.write_text(token)
        os.chmod(path, 0o600)
        return token

    def _rebuild(self) -> None:
        state = Worklog()
        for eid, payload in self._db.execute("select id, json from events order by id"):
            state.apply(eid, json.loads(payload))
        self.state = state

    def reads(self, worker: str) -> set[int]:
        return {r[0] for r in self._db.execute("select msg from reads where worker=?", (worker,))}

    def mark_read(self, worker: str, ids: list[int]) -> None:
        self._db.executemany("insert or ignore into reads values(?, ?)", [(worker, i) for i in ids])
        self._db.commit()

    # ------------------------------------------------------------- startup
    async def start(self, guild: discord.Guild) -> None:
        self.channel = await self._find_channel(guild)
        await self.lock(guild)
        self._rebuild()
        after = self.state.last_id
        async for msg in self.channel.history(
            limit=None, after=discord.Object(id=after) if after else None, oldest_first=True
        ):
            self._store(msg)
        self._rebuild()
        self.live_from = self.state.last_id
        self.ready.set()
        log.info("worklog ready in #%s: %d events, %d tasks, %d workers",
                 self.channel.name, len(self.state.history), len(self.state.tasks),
                 len(self.state.workers))
        asyncio.get_running_loop().create_task(self._duty_loop())
        asyncio.get_running_loop().create_task(self._after_restart())
        await self._serve()

    async def _find_channel(self, guild: discord.Guild) -> discord.TextChannel:
        # Two bots may create it at once: everyone uses the oldest one.
        found = sorted((c for c in guild.text_channels if c.name == config.WORKLOG_CHANNEL),
                       key=lambda c: c.id)
        if found:
            return found[0]
        log.info("creating #%s", config.WORKLOG_CHANNEL)
        await guild.create_text_channel(
            config.WORKLOG_CHANNEL,
            topic="Shared worklog for the agents on every machine. Written by the harness bots; "
                  "use `wl` (agents) or /worklog to read it.",
        )
        await asyncio.sleep(2)
        return sorted((c for c in guild.text_channels if c.name == config.WORKLOG_CHANNEL),
                      key=lambda c: c.id)[0]

    async def lock(self, guild: discord.Guild) -> None:
        """Hide #worklog from people (it's for the bots) and keep it open to every bot.

        Every harness bot does this at startup, and again when a bot joins (its
        role is created), so each bot's own role is always allowed in. The
        server owner and administrators still see it: Discord shows them every
        channel, so they mute it instead.
        """
        channel = self.channel
        if not hasattr(channel, "overwrites"):  # a test stand-in
            return
        hidden = discord.PermissionOverwrite(view_channel=False)
        bots = discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                           read_message_history=True)
        current = channel.overwrites
        # Let every bot in first, and hide it from @everyone only if that all
        # worked: hiding first would shut out a bot (this one included) whose
        # role isn't allowed yet.
        try:
            for role in guild.roles:
                if role.is_bot_managed() and current.get(role) != bots:
                    await channel.set_permissions(role, overwrite=bots)
            if current.get(guild.default_role) != hidden:
                await channel.set_permissions(guild.default_role, overwrite=hidden)
        except discord.HTTPException as e:
            log.warning("left #%s visible: couldn't set its permissions (the bot needs "
                        "Manage Roles): %s", channel.name, e)

    async def resync(self) -> None:
        """Pick up events posted while this bot was disconnected."""
        if not self.ready.is_set() or self.channel is None:
            return
        async for msg in self.channel.history(
            limit=None, after=discord.Object(id=self.state.last_id), oldest_first=True
        ):
            await self.ingest(msg)

    def _store(self, msg: discord.Message) -> dict | None:
        if not msg.author.bot:
            return None
        ev = decode(msg.content)
        if ev is None:
            return None
        ev.setdefault("via", msg.author.id)  # which bot posted it
        self._db.execute("insert or ignore into events values(?, ?)", (msg.id, json.dumps(ev)))
        self._db.commit()
        return ev

    # ------------------------------------------------------------- events
    async def ingest(self, msg: discord.Message) -> None:
        """A message in #worklog, from the gateway or our own post."""
        if not self.ready.is_set() or msg.id in self.state.results:
            return
        ev = self._store(msg)
        if ev is None:
            return
        if msg.id < self.state.last_id:  # arrived out of order: replay everything
            self._rebuild()
            return
        before = len(self.state.messages)
        ok, _ = self.state.apply(msg.id, ev)
        if ok and msg.id > self.live_from:
            await self._side_effects(ev, self.state.messages[before:])

    async def post(self, ev: dict, settle: float = SETTLE) -> tuple[bool, str]:
        """Post an event, wait for it to settle, and report its outcome."""
        assert self.channel is not None
        async with self._post_lock:
            msg = await self.channel.send(encode(ev, summary(ev)))
        await self.ingest(msg)
        await asyncio.sleep(settle)
        ok, text = self.state.results.get(msg.id, (False, "not applied"))
        if not ok:
            try:  # so the #worklog line doesn't read as if it happened
                await msg.edit(content=encode(ev, f"❌ refused: {summary(ev)} ({text})"))
            except discord.HTTPException:
                pass
        return ok, text

    async def _side_effects(self, ev: dict, new_messages: list) -> None:
        mine = self.my_workers()
        if ev.get("op") == "checkin" and ev.get("by") in mine:
            await self.notify(ev["by"], f"📋 #{ev.get('task')} · {ev.get('text', '').strip()}")
        for m in new_messages:
            if m.to in mine:
                if self._may_wake(m.to):
                    await self.deliver(m.to, m.sender, (f"(re #{m.task}) " if m.task else "") + m.text)
                else:
                    log.info("worklog: %s has had %d deliveries this hour; message left in inbox",
                             m.to, WAKE_LIMIT)

    def _may_wake(self, worker: str) -> bool:
        now = time.time()
        times = self._wakes.setdefault(worker, deque())
        while times and now - times[0] > 3600:
            times.popleft()
        if len(times) >= WAKE_LIMIT:
            return False
        times.append(now)
        return True

    # ------------------------------------------------------------- harness duties
    def my_workers(self) -> set[str]:
        me = self.client.user.id if self.client.user else 0
        return {w.name for w in self.state.workers.values() if w.bot == me}

    def worker_name(self, instance_name: str) -> str:
        from .bot import _slug  # local import: bot imports this module

        return f"{_slug(instance_name)}@{self.machine}"

    async def register(self, worker: str, channel_id: int, session: str, desc: str) -> None:
        """Register (or update) a worker this bot runs, if anything changed."""
        if not self.ready.is_set():
            return
        w = self.state.workers.get(worker)
        me = self.client.user.id
        if w and (w.bot, w.channel, w.machine, w.session, w.desc) == (me, channel_id, self.machine,
                                                                      session, desc):
            return
        await self.post({"op": "register", "by": worker, "bot": me, "channel": channel_id,
                         "machine": self.machine, "session": session, "desc": desc})

    def orphaned_subagents(self) -> dict[str, list]:
        """For each agent of this bot, the tasks its subagents (`<agent>/sub-N`) still hold. At startup these
        subagents are gone: they ran inside the agent's process, which the restart stopped."""
        mine = self.my_workers()
        out: dict[str, list] = {}
        for task in sorted(self.state.tasks.values(), key=lambda t: t.num):
            parent = task.owner.split("/")[0]
            if task.status == "claimed" and "/" in task.owner and parent in mine:
                out.setdefault(parent, []).append(task)
        return out

    async def _after_restart(self, delay: float = 45) -> None:
        # After the resume turns have started, so the message goes into them where there is one.
        await asyncio.sleep(delay)
        for worker, tasks in self.orphaned_subagents().items():
            lines = "\n".join(f"- #{t.num} {t.title} (held by {t.owner}, last check-in {fmt_time(t.checkin_at)})"
                              for t in tasks)
            log.info("worklog: telling %s about %d subagent claims cut off by the restart", worker, len(tasks))
            await self.deliver(worker, "harness", (
                "The harness restarted, and that stopped your background subagents. "
                f"They still hold these claims:\n{lines}\n"
                "For each one, start a new subagent on it at once, with the same `--as` name, and tell it to "
                "read the task notes and continue. If you don't continue a task, release it: "
                '`wl release <id> --as <name>`. Run long jobs as systemd units, not inside a subagent, '
                "so a restart does not stop them."))

    async def _duty_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                await self._check_claims()
                if config.WORKLOG_OFFERS:
                    await self._offer_work()
            except Exception:
                log.exception("worklog duty check failed")

    def _offer_plan(self, now: float) -> list[tuple[str, object]]:
        """(worker, task) offers to make now: each idle agent of this bot with no claim gets the best open,
        unblocked task in its own areas (folder, name, `wl areas`) that it hasn't passed on, if no other agent
        was offered that task in the last OFFER_WAIT. Tasks with no area are offered to nobody."""
        if self.idle_for is None:
            return []
        idle = []
        for w in sorted(self.my_workers()):
            if self.state.owned_by(w) or now - self._offered.get(w, 0) < OFFER_GAP:
                continue
            quiet = self.idle_for(w)
            if quiet is not None and quiet >= OFFER_AFTER:
                idle.append(w)
        plan, taken = [], set()
        for w in idle:
            # Only tasks in this agent's own project areas: other projects' work would fill its context.
            keys = area_keys(self.state.workers[w], config.WORKLOG_AREA_ALIASES) if w in self.state.workers else set()
            choices = [t for t in self.state.ready(w) if t.num not in taken and area_matches(t.area, keys)
                       and now - t.created >= OFFER_NEW_AFTER
                       and not (t.num in self._offers and self._offers[t.num][0] != w
                                and now - self._offers[t.num][1] < OFFER_WAIT)]
            if choices:
                plan.append((w, choices[0]))
                taken.add(choices[0].num)
        return plan

    async def _offer_work(self) -> None:
        now = time.time()
        for worker, task in self._offer_plan(now):
            if not self._may_wake(worker):
                continue
            self._offers[task.num] = (worker, now)
            self._offered[worker] = now
            log.info("worklog: offering #%d to idle %s", task.num, worker)
            desc = f"\n{task.desc[:600]}" if task.desc else ""
            await self.deliver(worker, "harness:offer", (
                f"You have no claimed task, and #{task.num} (p{task.prio}"
                + (f", {task.area}" if task.area else "") + f") is open: {task.title}{desc}\n"
                f'If you can do it, claim it (`wl claim {task.num} "<what you start>"`) and work on it, '
                "or give it to a subagent. If it isn't yours to do (other project, needs the user, "
                f'waits on other work), say why: `wl pass {task.num} "<why>"`, or '
                f"`wl depends {task.num} --on <tasks>`. Then stop; don't start other work."))

    def _orphans(self, now: float) -> list:
        """Lapsed claims of workers no harness bot runs (e.g. terminal sessions)
        that this bot should release: the bot that posted the claim or latest
        check-in does it; if it doesn't within another CHECKIN_EVERY (it's down,
        or the claim predates this rule), any bot does, and a duplicate release
        is simply refused."""
        me = self.client.user.id if self.client.user else 0
        out = []
        for task in self.state.tasks.values():
            if not task.lapsed(now):
                continue
            w = self.state.workers.get(task.owner)
            if w and w.bot:
                continue  # its own bot releases it (below)
            if task.via == me or now - task.checkin_at > 2 * CHECKIN_EVERY:
                out.append(task)
        return out

    async def _release(self, task, worker: str, now: float) -> bool:
        age = now - task.checkin_at
        log.info("worklog: releasing #%d from %s (no check-in for %dm)", task.num, worker, age // 60)
        ok, _ = await self.post({"op": "release", "by": f"harness@{self.machine}", "task": task.num,
                                 "harness": True, "text": f"no check-in from {worker} for "
                                                          f"{CHECKIN_EVERY // 60} min"})
        return ok

    async def _check_claims(self) -> None:
        now = time.time()
        for task in self._orphans(now):
            worker = task.owner
            if not await self._release(task, worker, now) or self.state.tasks[task.num].owner:
                continue
            await self.post({"op": "send", "by": f"harness@{self.machine}", "to": worker,
                             "task": task.num, "text": (
                                 f"#{task.num} was released: no check-in for {CHECKIN_EVERY // 60} min. "
                                 f"Claim it again (wl claim {task.num}) if you're still on it, and "
                                 f'check in every 30 min: wl checkin {task.num} "<up to 10 words>"')})
        for worker in self.my_workers():
            for task in self.state.owned_by(worker):
                age = now - task.checkin_at
                if age > CHECKIN_EVERY:
                    await self._release(task, worker, now)
                    await self.notify(worker, f"⌛ #{task.num} released: no check-in for "
                                              f"{CHECKIN_EVERY // 60} min")
                elif age > REMIND_AT and (task.num, task.checkin_at) not in self._reminded:
                    self._reminded.add((task.num, task.checkin_at))
                    await self.deliver(worker, "harness", (
                        f"Check in on #{task.num} within 5 minutes or the claim is released: "
                        f'wl checkin {task.num} "<up to 10 words of progress>"'
                    ))

    def env_for(self, worker: str) -> dict[str, str]:
        """Environment for an agent's CLI, so `wl` works and knows who it is."""
        return {
            "WORKLOG_WORKER": worker,
            "WORKLOG_URL": self.url,
            "WORKLOG_TOKEN": self.token,
            "PATH": f"{config.BASE_DIR / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}",
        }

    # ------------------------------------------------------------- local API
    async def _serve(self) -> None:
        app = web.Application()
        app.router.add_post("/wl", self._handle)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", config.WORKLOG_PORT).start()
        log.info("worklog API on %s", self.url)

    async def _handle(self, request: web.Request) -> web.Response:
        if not secrets.compare_digest(request.headers.get("X-Worklog-Token", ""), self.token):
            return web.json_response({"ok": False, "text": "bad worklog token"}, status=403)
        if not self.ready.is_set():
            return web.json_response({"ok": False, "text": "the worklog is still starting; retry"})
        body = await request.json()
        ok, text = await self.run(body.get("argv") or [], str(body.get("worker") or ""))
        return web.json_response({"ok": ok, "text": text})

    async def run(self, argv: list[str], worker: str) -> tuple[bool, str]:
        """Run a `wl` command as `worker` (also used by /worklog)."""
        if argv[:1] == ["import-legacy"]:
            return await self.import_legacy(argv[1:])
        if argv[:1] == ["wait"] and not self._has_harness(worker):
            await self._wait_for_mail(worker, argv)
        try:
            text, ev, read = run_command(self.state, argv, worker, self.reads(worker))
        except CommandError as e:
            return False, str(e)
        if read:
            self.mark_read(worker, read)
        if ev is None:
            return True, text
        ok, outcome = await self.post(ev)
        if ok and ev["op"] == "claim":
            outcome += self._overlaps(ev["task"])
        return ok, outcome

    def _has_harness(self, worker: str) -> bool:
        w = self.state.workers.get(worker)
        return bool(w and w.bot)

    async def _wait_for_mail(self, worker: str, argv: list[str]) -> None:
        """`wl wait` for a session no harness bot delivers to (e.g. a terminal):
        block until it has unread mail, or the timeout (default and cap: 1 h)."""
        timeout = 3600.0
        if "--timeout" in argv:
            try:
                timeout = min(timeout, float(argv[argv.index("--timeout") + 1]))
            except (IndexError, ValueError):
                pass
        end = time.monotonic() + timeout
        while time.monotonic() < end and not self.state.inbox(worker, self.reads(worker)):
            await asyncio.sleep(2)

    async def import_legacy(self, args: list[str]) -> tuple[bool, str]:
        """Copy a per-project worklog DB (ops/worklog.py's SQLite) into this one.

        wl import-legacy PATH [--map old=new ...]; only into an empty worklog,
        so task numbers stay the same.
        """
        if not args:
            return False, "usage: wl import-legacy PATH [--map old=new ...]"
        if self.state.tasks:
            return False, "the shared worklog already has tasks; import only into an empty one"
        rename = dict(a.split("=", 1) for a in args[args.index("--map") + 1:] if "=" in a) \
            if "--map" in args else {}
        name = lambda n: rename.get(n, n) if n else ""  # noqa: E731
        db = sqlite3.connect(f"file:{os.path.expanduser(args[0])}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        now = time.time()
        events: list[dict] = []
        for w in db.execute("select * from workers order by registered"):
            events.append({"op": "register", "by": name(w["name"]), "session": w["session"] or "",
                           "desc": f"(imported) {w['descr'] or ''}".strip()})
        tasks = list(db.execute("select * from tasks order by id"))
        num = {t["id"]: i for i, t in enumerate(tasks, 1)}
        for t in tasks:
            events.append({"op": "add", "by": name(t["created_by"]) or "import", "title": t["title"],
                           "desc": t["descr"] or "", "area": t["area"] or "", "files": t["files"] or "",
                           "prio": t["prio"] or 2})
        for n in db.execute("select * from notes order by id"):
            if n["task"] in num:
                events.append({"op": "note", "by": name(n["author"]), "task": num[n["task"]],
                               "text": f"[{time.strftime('%m-%d %H:%M', time.localtime(n['ts']))}] {n['text']}"})
        for t in tasks:
            owner = name(t["owner"])
            if t["status"] == "claimed" and owner:
                events.append({"op": "claim", "by": owner, "task": num[t["id"]], "text": "imported claim"})
            elif t["status"] in ("done", "blocked", "dropped"):
                op = {"done": "done", "blocked": "block", "dropped": "drop"}[t["status"]]
                events.append({"op": op, "by": owner or name(t["created_by"]) or "import",
                               "task": num[t["id"]], "text": t["result"] or "", "force": True})
        for l in db.execute("select * from leases where expires > ?", (now,)):
            events.append({"op": "lease", "by": name(l["owner"]), "res": l["resource"],
                           "ttl": int(l["expires"] - now), "why": l["why"] or ""})
        for m in db.execute("select * from messages where read_ts is null order by id"):
            events.append({"op": "send", "by": name(m["sender"]), "to": name(m["recipient"]),
                           "text": m["text"], "task": num.get(m["task"])})
        refused = []
        for ev in events:  # nothing competes with an import: no settling wait
            ok, text = await self.post(ev, settle=0)
            if not ok:
                refused.append(f"{ev['op']} by {ev['by']}: {text}")
        return not refused, (f"imported {len(events)} events: {len(tasks)} tasks"
                             + "".join(f"\n  refused: {r}" for r in refused))

    def _overlaps(self, num: int) -> str:
        task = self.state.tasks.get(num)
        files = {f.strip() for f in (task.files if task else "").split(",") if f.strip()}
        out = []
        for other in self.state.tasks.values():
            shared = files & {f.strip() for f in other.files.split(",") if f.strip()}
            if other.num != num and other.status == "claimed" and shared:
                out.append(f"\n  warning: #{other.num} ({other.owner}) also touches {', '.join(sorted(shared))}")
        return "".join(out)

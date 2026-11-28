"""Read-only worklog snapshots and a self-contained dashboard page."""

from __future__ import annotations

import re
import time

from .worklog import Worklog

HISTORY_LIMIT = 100


def _entry(item: tuple[float, str, str]) -> dict:
    at, by, text = item
    return {"at": at, "by": by, "text": text}


# User to-do groups, in match order: a blocked task goes in the first group whose
# pattern matches its block reason. Each pattern is matched case-insensitively.
TODO_GROUPS: list[tuple[str, str, str]] = [
    ("approve", "Give a go-ahead", r"go-ahead|approv|\buser ok\b|needs user ok"),
    ("run", "Run a command", r"\bsudo\b|administrator|\badmin\b|powershell|\brun this\b|\brun:|authorized_keys"),
    ("device", "Test on the phone", r"\bphone\b|pixel|\bapk\b|on-device|\bdevice\b|\badb\b"),
    ("hardware", "Turn on or connect hardware", r"\bps5\b|console|awake|powered|plug"),
    ("accounts", "Set up accounts or credentials", r"account|credential|\btoken\b|sign-in|api key|\.env\b"),
    ("decide", "Decide or review", r"decision|decide|review|\bpick\b|choose|approval"),
    ("later", "Parked: revisit later", r"revisit|waits for (a|real)|\bfuture\b|low priority"),
    ("other", "Check why it is blocked", r""),
]
_TODO_RES = [(key, title, re.compile(pattern, re.I)) for key, title, pattern in TODO_GROUPS]
# The command a block reason gives to run: the text after the last "run:" or "run this one line:".
_COMMAND_RE = re.compile(r"\brun(?: this(?: one)?(?: line)?)?:\s*(.+)$", re.I | re.S)
SUMMARY_LIMIT = 140


_NEED_RE = re.compile(r"\b(needs?|wait(s|ing)?|requires?)\b", re.I)


def _summary(text: str) -> str:
    """The clause of a block reason that says what is needed, short enough for one line.

    It is the first clause with "need", "wait" or "require"; else the first clause.
    """
    clauses = [c.rstrip(".;") for c in re.split(r"(?<=[.;])\s", " ".join(text.split())) if c.strip(" .;")]
    if not clauses:
        return ""
    first = next((c for c in clauses if _NEED_RE.search(c)), clauses[0])
    first = first[:1].upper() + first[1:]
    if len(first) > SUMMARY_LIMIT:
        first = first[: SUMMARY_LIMIT - 1].rstrip() + "…"
    return first


def todo(state: Worklog) -> list[dict]:
    """Blocked tasks that wait for the user, grouped by the kind of action.

    A task that waits only for unfinished tasks it depends on is left out:
    the agents, not the user, must finish those first.
    """
    finished = {"done", "dropped"}
    items: dict[str, list[dict]] = {key: [] for key, _, _ in TODO_GROUPS}
    for task in state.tasks.values():
        if task.status != "blocked":
            continue
        waiting = [n for n in task.after if n in state.tasks and state.tasks[n].status not in finished]
        if waiting:
            continue
        reason = (task.result or task.progress or "").strip()
        key = next(key for key, _, rx in _TODO_RES if not rx.pattern or rx.search(reason))
        command = ""
        match = _COMMAND_RE.search(reason) if key == "run" else None
        if match:
            command = match.group(1).strip()
            action = _summary(reason[: match.start()]) or "Run the command"
        else:
            action = _summary(reason) or "No block reason given"
        items[key].append({
            "num": task.num,
            "title": task.title,
            "area": task.area,
            "owner": task.owner,
            "prio": task.prio,
            "action": action,
            "command": command,
            "reason": reason,
        })
    groups = []
    for key, title, _ in TODO_GROUPS:
        if items[key]:
            groups.append({
                "key": key,
                "title": title,
                "items": sorted(items[key], key=lambda item: (item["prio"], item["num"])),
            })
    return groups


def snapshot(state: Worklog, machine: str, now: float | None = None) -> dict:
    generated_at = time.time() if now is None else now
    threads: dict[int, list[dict]] = {}
    for message in state.messages:
        if message.task is not None:
            threads.setdefault(message.task, []).append({
                "id": message.id,
                "sender": message.sender,
                "to": message.to,
                "at": message.at,
                "text": message.text,
            })
    tasks = []
    for task in sorted(state.tasks.values(), key=lambda item: item.num):
        tasks.append({
            "num": task.num,
            "title": task.title,
            "desc": task.desc,
            "area": task.area,
            "files": task.files,
            "prio": task.prio,
            "status": task.status,
            "owner": task.owner,
            "created_by": task.created_by,
            "created": task.created,
            "updated": task.updated,
            "checkin_at": task.checkin_at,
            "progress": task.progress,
            "result": task.result,
            "after": sorted(task.after),
            "notes": [_entry(note) for note in task.notes],
            "thread": threads.get(task.num, []),
        })
    workers = [{
        "name": worker.name,
        "machine": worker.machine,
        "desc": worker.desc,
        "seen": worker.seen,
        "areas": sorted(worker.areas),
    } for worker in sorted(state.workers.values(), key=lambda item: item.name)]
    leases = [{
        "resource": lease.resource,
        "owner": lease.owner,
        "why": lease.why,
        "since": lease.since,
        "expires": lease.expires,
    } for lease in sorted(state.leases.values(), key=lambda item: item.resource)]
    return {
        "generated_at": generated_at,
        "machine": machine,
        "tasks": tasks,
        "todo": todo(state),
        "workers": workers,
        "leases": leases,
        "history": [_entry(item) for item in state.history[-HISTORY_LIMIT:]],
    }


def page() -> str:
    return _PAGE


_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Worklog dashboard</title>
<style>
:root {
  color-scheme: dark;
  --bg: #090d14;
  --panel: rgba(18, 25, 38, .88);
  --panel-strong: #151e2d;
  --line: #283449;
  --text: #edf3fc;
  --muted: #93a4bb;
  --accent: #70d7bd;
  --accent-strong: #a0f0dc;
  --blue: #83b8ff;
  --amber: #f4c876;
  --red: #ff9b9b;
  --radius: 14px;
}
* { box-sizing: border-box; }
[hidden] { display: none !important; }
body {
  margin: 0;
  min-width: 320px;
  background:
    radial-gradient(circle at 12% -10%, rgba(54, 108, 132, .22), transparent 34rem),
    radial-gradient(circle at 95% 0%, rgba(64, 75, 140, .16), transparent 30rem),
    var(--bg);
  color: var(--text);
  font: 14px/1.5 Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
button, input, select, textarea { font: inherit; }
button.stat { color: inherit; text-align: left; cursor: pointer; }
button, input[type="search"], select, textarea {
  border: 1px solid var(--line);
  border-radius: 9px;
  color: var(--text);
  background: #0e1623;
}
input[type="search"], select { width: 100%; padding: 10px 12px; }
input[type="search"]::placeholder, textarea::placeholder { color: #718299; }
button:focus-visible, input:focus-visible, select:focus-visible, textarea:focus-visible, summary:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 3px;
}
.shell { width: min(1440px, 100%); margin: 0 auto; padding: 30px clamp(16px, 3vw, 42px) 40px; }
.topbar { display: flex; align-items: flex-start; justify-content: space-between; gap: 24px; margin-bottom: 24px; }
.eyebrow { margin: 0 0 5px; color: var(--accent); font-size: 11px; font-weight: 750; letter-spacing: .16em; text-transform: uppercase; }
h1 { margin: 0; font-size: clamp(27px, 4vw, 38px); line-height: 1.15; letter-spacing: -.035em; }
.subtitle { margin: 8px 0 0; color: var(--muted); }
.live-meta { min-width: 210px; text-align: right; color: var(--muted); font-size: 12px; }
.live-dot { display: inline-block; width: 7px; height: 7px; margin-right: 7px; border-radius: 50%; background: var(--accent); box-shadow: 0 0 12px rgba(112, 215, 189, .6); }
.machine { margin-bottom: 6px; color: var(--text); font-weight: 650; overflow-wrap: anywhere; }
.controls {
  display: grid;
  grid-template-columns: minmax(220px, 1fr) auto auto;
  align-items: end;
  gap: 12px;
  padding: 16px;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--panel);
  box-shadow: 0 14px 40px rgba(0, 0, 0, .16);
}
.control-label { display: grid; gap: 6px; color: var(--muted); font-size: 12px; font-weight: 650; }
.refresh { min-height: 41px; padding: 9px 17px; border-color: #3d7f73; background: #193b37; cursor: pointer; font-weight: 700; }
.refresh:hover { background: #225049; }
.refresh:disabled { cursor: wait; opacity: .6; }
.auto { display: flex; align-items: center; gap: 8px; min-height: 41px; color: var(--muted); white-space: nowrap; cursor: pointer; font-size: 12px; }
.auto input { width: 16px; height: 16px; accent-color: var(--accent); }
.notice { margin: 14px 0 0; padding: 12px 15px; border: 1px solid #70454d; border-radius: 10px; background: rgba(93, 39, 47, .28); color: var(--red); }
.loading { color: var(--muted); }
.stats { display: grid; grid-template-columns: repeat(6, minmax(90px, 1fr)); gap: 10px; margin: 18px 0; }
.stat { padding: 13px 15px; border: 1px solid var(--line); border-radius: 11px; background: rgba(18, 25, 38, .68); }
.stat-value { display: block; font-size: 24px; font-weight: 750; line-height: 1.15; }
.stat:hover { border-color: #4b5b75; }
.stat.status-chip[aria-pressed="false"] { opacity: .45; border-style: dashed; }
.stat.status-chip[aria-pressed="true"] { border-color: var(--accent); background: rgba(47, 119, 98, .16); }
.stat-label { display: block; margin-top: 5px; color: var(--muted); font-size: 11px; letter-spacing: .06em; text-transform: uppercase; }
.todo { margin: 0 0 18px; padding: 16px; border: 1px solid #3c7d6e; border-radius: var(--radius); background: rgba(22, 48, 44, .45); }
.todo .section-head { margin: 0 0 12px; }
.todo-groups { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 12px; align-items: start; }
.todo-group { border: 1px solid var(--line); border-radius: 11px; background: rgba(14, 22, 35, .85); }
.todo-group > summary { display: flex; align-items: center; gap: 8px; padding: 10px 13px; cursor: pointer; list-style: none; font-weight: 720; }
.todo-group > summary::-webkit-details-marker { display: none; }
.todo-group > summary .area-total { margin-left: auto; }
.todo-items { margin: 0; padding: 0 13px 10px; list-style: none; }
.todo-item { padding: 8px 0; border-top: 1px solid rgba(40, 52, 73, .7); }
.todo-item a { color: var(--blue); font-weight: 700; text-decoration: none; font-variant-numeric: tabular-nums; }
.todo-item a:hover { text-decoration: underline; }
.todo-action { overflow-wrap: anywhere; }
.todo-meta { color: var(--muted); font-size: 11px; overflow-wrap: anywhere; }
.todo-command { display: flex; gap: 6px; align-items: flex-start; margin-top: 6px; }
.todo-command code { flex: 1; min-width: 0; padding: 6px 8px; border: 1px solid var(--line); border-radius: 7px; background: #0a111b; font-size: 11px; white-space: pre-wrap; overflow-wrap: anywhere; }
.copy { flex: none; padding: 4px 9px; cursor: pointer; font-size: 11px; }
.task.flash { border-color: var(--accent); box-shadow: 0 0 0 2px rgba(112, 215, 189, .35); }
.content { display: grid; grid-template-columns: minmax(0, 1fr) 330px; align-items: start; gap: 18px; }
.section-head { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; margin: 8px 2px 12px; }
h2 { margin: 0; font-size: 17px; letter-spacing: -.01em; }
.section-count { color: var(--muted); font-size: 12px; }
.area { margin-bottom: 12px; border: 1px solid var(--line); border-radius: var(--radius); background: var(--panel); overflow: hidden; }
.area > summary { display: flex; align-items: center; gap: 10px; padding: 14px 17px; cursor: pointer; list-style: none; background: rgba(26, 36, 53, .75); }
.area > summary::-webkit-details-marker, .task > summary::-webkit-details-marker { display: none; }
.disclosure { width: 14px; color: var(--muted); transition: transform .15s ease; }
details[open] > summary > .disclosure { transform: rotate(90deg); }
.area-name { flex: 1; min-width: 0; font-weight: 720; overflow-wrap: anywhere; }
.area-total { color: var(--muted); font-size: 12px; }
.area-tasks { padding: 10px; }
.task { border: 1px solid transparent; border-radius: 10px; background: #101824; }
.task + .task { margin-top: 8px; }
.task[open] { border-color: var(--line); }
.task > summary { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 9px 14px; padding: 13px 14px; cursor: pointer; list-style: none; }
.task-heading { display: flex; align-items: center; gap: 9px; min-width: 0; }
.task-title { min-width: 0; font-weight: 680; overflow-wrap: anywhere; }
.task-number { color: var(--blue); font-variant-numeric: tabular-nums; }
.status { flex: none; padding: 2px 7px; border: 1px solid #44516a; border-radius: 999px; color: #bac7da; font-size: 10px; font-weight: 800; letter-spacing: .06em; text-transform: uppercase; }
.status.open { border-color: #3f6e91; color: var(--blue); background: rgba(52, 105, 145, .12); }
.status.claimed { border-color: #3c7d6e; color: var(--accent-strong); background: rgba(47, 119, 98, .13); }
.status.blocked { border-color: #865a45; color: var(--amber); background: rgba(133, 82, 46, .12); }
.status.done { border-color: #426451; color: #9bc7a8; background: rgba(58, 105, 72, .12); }
.status.dropped { color: #8895a8; }
.priority { color: var(--muted); font-size: 12px; white-space: nowrap; }
.latest { grid-column: 1 / -1; margin: 0; color: var(--muted); font-size: 12px; overflow-wrap: anywhere; white-space: pre-wrap; }
.task-body { padding: 2px 16px 16px; border-top: 1px solid rgba(40, 52, 73, .65); }
.task-body h3, .side-section h3 { margin: 16px 0 7px; color: var(--muted); font-size: 11px; letter-spacing: .09em; text-transform: uppercase; }
.description { margin: 0; color: #d5dfec; overflow-wrap: anywhere; white-space: pre-wrap; }
.fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px 18px; margin: 16px 0 0; }
.field { min-width: 0; padding: 9px 11px; border-radius: 8px; background: rgba(8, 13, 21, .38); }
.field.wide { grid-column: 1 / -1; }
.field-label { display: block; margin-bottom: 3px; color: var(--muted); font-size: 10px; letter-spacing: .06em; text-transform: uppercase; }
.field-value { display: block; overflow-wrap: anywhere; white-space: pre-wrap; }
.notes { margin: 0; padding: 0; list-style: none; }
.note { padding: 10px 0; border-top: 1px solid rgba(40, 52, 73, .65); }
.note-text { margin: 0 0 4px; overflow-wrap: anywhere; white-space: pre-wrap; }
.note-meta { color: var(--muted); font-size: 11px; }
.thread-message { padding: 10px 12px; border-left: 2px solid #3d7f73; border-radius: 0 8px 8px 0; background: rgba(8, 13, 21, .38); }
.thread-message + .thread-message { margin-top: 8px; }
.thread-message p { margin: 0 0 5px; overflow-wrap: anywhere; white-space: pre-wrap; }
.reply-form { display: grid; gap: 9px; margin-top: 12px; }
.reply-form textarea { width: 100%; min-height: 84px; padding: 10px 12px; resize: vertical; }
.reply-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; }
.reply-submit { padding: 8px 14px; border-color: #3d7f73; background: #193b37; cursor: pointer; font-weight: 700; }
.drop-task { margin-left: auto; padding: 8px 14px; border-color: #70454d; background: rgba(93, 39, 47, .35); color: var(--red); cursor: pointer; font-weight: 700; }
.drop-task:hover { background: rgba(120, 45, 55, .5); }
.drop-task:disabled { cursor: not-allowed; opacity: .6; }
.reply-submit:disabled, .reply-form textarea:disabled { cursor: not-allowed; opacity: .6; }
.reply-status { margin: 0; color: var(--muted); font-size: 12px; overflow-wrap: anywhere; }
.reply-status.error { color: var(--red); }
.reply-status.success { color: var(--accent-strong); }
.aside { display: grid; gap: 14px; }
.side-section { padding: 16px; border: 1px solid var(--line); border-radius: var(--radius); background: var(--panel); }
.side-section .section-head { margin: 0 0 11px; }
.worker, .lease, .history-item { padding: 11px 0; border-top: 1px solid var(--line); }
.worker:first-child, .lease:first-child, .history-item:first-child { border-top: 0; padding-top: 2px; }
.worker-name, .lease-name { font-weight: 680; overflow-wrap: anywhere; }
.worker-desc, .lease-why, .history-text { margin: 4px 0; color: #c6d2e1; overflow-wrap: anywhere; white-space: pre-wrap; }
.item-meta { color: var(--muted); font-size: 11px; overflow-wrap: anywhere; }
.tags { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 7px; }
.tag { padding: 2px 6px; border-radius: 5px; background: #202c3f; color: #b8c7da; font-size: 10px; }
.empty { margin: 12px 0; padding: 22px 16px; border: 1px dashed #334158; border-radius: 10px; color: var(--muted); text-align: center; }
.side-section .empty { padding: 14px 8px; border: 0; }
footer { margin-top: 22px; color: #718299; font-size: 11px; text-align: center; }
@media (max-width: 1050px) {
  .controls { grid-template-columns: minmax(200px, 1fr) auto; }
  .auto { grid-column: 1 / -1; min-height: auto; }
  .content { grid-template-columns: minmax(0, 1fr) 280px; }
  .stats { grid-template-columns: repeat(3, 1fr); }
}
@media (max-width: 780px) {
  .shell { padding-top: 20px; }
  .topbar { display: block; }
  .live-meta { margin-top: 14px; text-align: left; }
  .content { grid-template-columns: 1fr; }
  .aside { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .side-section:last-child { grid-column: 1 / -1; }
}
@media (max-width: 560px) {
  .controls { grid-template-columns: 1fr 1fr; padding: 13px; }
  .controls .search-control { grid-column: 1 / -1; }
  .refresh { width: 100%; }
  .auto { grid-column: 1 / -1; }
  .stats { grid-template-columns: repeat(2, 1fr); }
  .aside { grid-template-columns: 1fr; }
  .side-section:last-child { grid-column: auto; }
  .task-heading { flex-wrap: wrap; }
  .fields { grid-template-columns: 1fr; }
  .field.wide { grid-column: auto; }
}
</style>
</head>
<body>
<div class="shell">
<header class="topbar">
  <div>
    <p class="eyebrow">Operations · live view</p>
    <h1>Worklog dashboard</h1>
    <p class="subtitle">Tasks, ownership, leases, and recent activity across projects.</p>
  </div>
  <div class="live-meta">
    <div class="machine"><span class="live-dot"></span><span id="machine">Machine: —</span></div>
    <div id="generated">Waiting for state</div>
  </div>
</header>
<section id="todo" class="todo" aria-label="To do for you" hidden>
  <div class="section-head"><h2>To do for you</h2><span id="todo-count" class="section-count"></span></div>
  <div id="todo-groups" class="todo-groups"></div>
</section>
<section class="controls" aria-label="Dashboard controls">
  <label class="control-label search-control">Search tasks
    <input id="search" type="search" placeholder="Title, description, file, owner, note…" autocomplete="off">
  </label>
  <button id="refresh" class="refresh" type="button">Refresh</button>
  <label class="auto"><input id="auto-refresh" type="checkbox" checked> Auto-refresh every 10s</label>
</section>
<p id="error" class="notice" role="alert" hidden></p>
<p id="loading" class="loading">Loading worklog…</p>
<section id="stats" class="stats" aria-label="Task counts. Tap a status to show or hide its tasks. Tap Total to show all."></section>
<div class="content">
  <main>
    <div class="section-head"><h2>Tasks by area</h2><span id="visible-count" class="section-count"></span></div>
    <div id="areas"></div>
    <p id="no-matches" class="empty" hidden>No tasks match the current search and the selected statuses.</p>
  </main>
  <aside class="aside">
    <section class="side-section">
      <div class="section-head"><h2>Workers</h2><span id="worker-count" class="section-count"></span></div>
      <div id="workers"></div>
    </section>
    <section class="side-section">
      <div class="section-head"><h2>Leases</h2><span id="lease-count" class="section-count"></span></div>
      <div id="leases"></div>
    </section>
    <section class="side-section">
      <div class="section-head"><h2>Recent history</h2><span id="history-count" class="section-count"></span></div>
      <div id="history"></div>
    </section>
  </aside>
</div>
<footer>Same-origin state · Dynamic text is rendered without HTML interpretation.</footer>
</div>
<script>
(() => {
  'use strict';
  const search = document.getElementById('search');
  const STATUSES = ['open', 'claimed', 'blocked', 'done', 'dropped'];
  const DEFAULT_STATUSES = ['open', 'blocked'];
  const shownStatuses = new Set(DEFAULT_STATUSES);
  const refreshButton = document.getElementById('refresh');
  const autoRefresh = document.getElementById('auto-refresh');
  const errorBox = document.getElementById('error');
  const loading = document.getElementById('loading');
  const replyDrafts = new Map();
  const replyPending = new Set();
  const replyFeedback = new Map();
  const dropPending = new Set();
  const openTasks = new Set();
  const openAreas = new Map();
  let currentState = null;
  let timer = null;
  let inFlight = false;
  let hashHandled = false;

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = String(text);
    return element;
  }

  function empty(text) {
    return node('p', 'empty', text);
  }

  function formatTime(value) {
    const seconds = Number(value);
    if (!Number.isFinite(seconds) || seconds <= 0) return '—';
    const date = new Date(seconds * 1000);
    return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString();
  }

  function notesFor(task) {
    return Array.isArray(task.notes) ? task.notes : [];
  }

  function latestStatus(task) {
    const notes = notesFor(task);
    if (task.status === 'claimed' && task.progress) return task.progress;
    if (['done', 'blocked'].includes(task.status) && task.result) return task.result;
    if (notes.length) return notes[notes.length - 1].text;
    return task.progress || task.result || 'No status message yet.';
  }

  function field(label, value, wide = false) {
    const wrapper = node('div', wide ? 'field wide' : 'field');
    wrapper.append(node('span', 'field-label', label), node('span', 'field-value', value || '—'));
    return wrapper;
  }

  function threadFor(task) {
    return Array.isArray(task.thread) ? task.thread : [];
  }

  function replyForm(task) {
    const form = node('form', 'reply-form');
    const textarea = node('textarea');
    textarea.name = 'reply';
    textarea.required = true;
    textarea.maxLength = 2000;
    textarea.placeholder = task.owner ? 'Write a reply for ' + task.owner + '…' : 'No agent is assigned to this task.';
    textarea.value = replyDrafts.get(task.num) || '';
    textarea.disabled = !task.owner || replyPending.has(task.num);
    textarea.setAttribute('aria-label', 'Reply about task #' + task.num);
    textarea.addEventListener('input', () => {
      if (textarea.value) replyDrafts.set(task.num, textarea.value);
      else replyDrafts.delete(task.num);
      replyFeedback.delete(task.num);
    });
    const submit = node('button', 'reply-submit', replyPending.has(task.num) ? 'Sending…' : 'Send reply');
    submit.type = 'submit';
    submit.disabled = textarea.disabled;
    const feedback = replyFeedback.get(task.num);
    const status = node('p', 'reply-status' + (feedback ? ' ' + feedback.kind : ''));
    status.setAttribute('aria-live', 'polite');
    status.textContent = replyPending.has(task.num) ? 'Sending to the assigned agent…' :
      (feedback ? feedback.text : (task.owner ? 'The assigned agent is notified immediately.' : 'Assign an agent before sending a reply.'));
    const actions = node('div', 'reply-actions');
    actions.append(submit, status);
    if (!['done', 'dropped'].includes(task.status)) actions.append(dropButton(task, status));
    form.append(textarea, actions);
    form.addEventListener('submit', event => {
      event.preventDefault();
      void sendReply(task.num, textarea, submit, status);
    });
    return form;
  }

  function dropButton(task, status) {
    const button = node('button', 'drop-task', dropPending.has(task.num) ? 'Dropping…' : 'Drop task');
    button.type = 'button';
    button.disabled = dropPending.has(task.num);
    button.title = 'Drop #' + task.num + (task.owner ? ' and tell ' + task.owner + ' to stop work on it' : '');
    button.addEventListener('click', () => void dropTask(task, button, status));
    return button;
  }

  async function dropTask(task, button, status) {
    const reason = window.prompt('Drop #' + task.num + ' "' + (task.title || '') + '"?\n' +
      (task.owner ? task.owner + ' is told to stop work on it.\n' : '') + 'Reason (optional):', '');
    if (reason === null) return;
    dropPending.add(task.num);
    button.disabled = true;
    button.textContent = 'Dropping…';
    status.textContent = 'Dropping #' + task.num + '…';
    status.className = 'reply-status';
    try {
      const response = await fetch('drop' + window.location.search, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({task: task.num, reason: reason.trim()})
      });
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.text || ('Drop request returned HTTP ' + response.status));
      replyFeedback.set(task.num, {kind: 'success', text: '#' + task.num + ' dropped' + (task.owner ? '; ' + task.owner + ' was told.' : '.')});
      dropPending.delete(task.num);
      await refresh();
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      replyFeedback.set(task.num, {kind: 'error', text: 'Drop failed: ' + message});
      status.textContent = 'Drop failed: ' + message;
      status.className = 'reply-status error';
    } finally {
      dropPending.delete(task.num);
      button.disabled = false;
      button.textContent = 'Drop task';
    }
  }

  async function sendReply(num, textarea, submit, status) {
    const text = textarea.value.trim();
    if (!text) {
      status.textContent = 'Write a reply before sending.';
      status.className = 'reply-status error';
      return;
    }
    replyPending.add(num);
    replyFeedback.delete(num);
    textarea.disabled = true;
    submit.disabled = true;
    submit.textContent = 'Sending…';
    status.textContent = 'Sending to the assigned agent…';
    status.className = 'reply-status';
    try {
      const response = await fetch('reply' + window.location.search, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({task: num, text: text})
      });
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.text || ('Reply request returned HTTP ' + response.status));
      replyDrafts.delete(num);
      textarea.value = '';
      replyFeedback.set(num, {kind: 'success', text: 'Reply sent to the assigned agent.'});
      replyPending.delete(num);
      await refresh();
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      replyFeedback.set(num, {kind: 'error', text: 'Reply failed: ' + message});
      status.textContent = 'Reply failed: ' + message;
      status.className = 'reply-status error';
    } finally {
      replyPending.delete(num);
      const task = currentState && (currentState.tasks || []).find(item => item.num === num);
      textarea.disabled = !task || !task.owner;
      submit.disabled = textarea.disabled;
      submit.textContent = 'Send reply';
    }
  }

  function taskNode(task) {
    const details = node('details', 'task');
    details.id = 'task-' + task.num;
    details.open = openTasks.has(task.num);
    details.addEventListener('toggle', () => {
      if (details.open) openTasks.add(task.num);
      else openTasks.delete(task.num);
    });
    const summary = node('summary');
    const heading = node('div', 'task-heading');
    const knownStatus = ['open', 'claimed', 'blocked', 'done', 'dropped'].includes(task.status) ? task.status : 'other';
    heading.append(
      node('span', 'status ' + knownStatus, task.status || 'unknown'),
      node('span', 'task-title', '#' + task.num + ' ' + (task.title || '(untitled)'))
    );
    summary.append(heading, node('span', 'priority', 'Priority ' + task.prio));
    summary.append(node('p', 'latest', latestStatus(task)));
    details.append(summary);

    const body = node('div', 'task-body');
    body.append(node('h3', '', 'Description'));
    body.append(node('p', 'description', task.desc || 'No description.'));
    if (task.result) {
      body.append(node('h3', '', 'Result'));
      body.append(node('p', 'description', task.result));
    }
    const fields = node('div', 'fields');
    fields.append(
      field('Area', task.area || 'Unassigned'),
      field('Owner', task.owner || 'Unclaimed'),
      field('Created by', task.created_by),
      field('Depends on', (task.after || []).map(num => '#' + num).join(', ') || 'None'),
      field('Files', task.files, true),
      field('Created', formatTime(task.created)),
      field('Updated', formatTime(task.updated)),
      field('Last check-in', formatTime(task.checkin_at))
    );
    body.append(fields);
    const notes = notesFor(task);
    body.append(node('h3', '', 'Notes history · ' + notes.length));
    if (!notes.length) {
      body.append(empty('No notes for this task.'));
    } else {
      const list = node('ol', 'notes');
      notes.slice().reverse().forEach(note => {
        const item = node('li', 'note');
        item.append(node('p', 'note-text', note.text));
        item.append(node('div', 'note-meta', (note.by || 'Unknown worker') + ' · ' + formatTime(note.at)));
        list.append(item);
      });
      body.append(list);
    }
    const thread = threadFor(task);
    body.append(node('h3', '', 'Thread · ' + thread.length));
    if (!thread.length) {
      body.append(empty('No replies for this task.'));
    } else {
      thread.forEach(message => {
        const item = node('div', 'thread-message');
        item.append(node('p', '', message.text));
        item.append(node('div', 'note-meta',
          (message.sender || 'Unknown sender') + ' → ' + (message.to || 'Unknown recipient') + ' · ' + formatTime(message.at)));
        body.append(item);
      });
    }
    body.append(node('h3', '', 'Reply'));
    body.append(replyForm(task));
    details.append(body);
    return details;
  }

  function searchText(task) {
    const notes = notesFor(task).map(note => [note.by, note.text]);
    const thread = threadFor(task).map(message => [message.sender, message.to, message.text]);
    return [
      task.num, task.title, task.desc, task.area, task.files, task.status, task.owner,
      task.created_by, task.progress, task.result, task.after, notes, thread
    ].flat(3).join(' ').toLowerCase();
  }

  function renderStats(tasks) {
    const stats = document.getElementById('stats');
    const labels = [['total', 'Total'], ['open', 'Open'], ['claimed', 'Claimed'], ['blocked', 'Blocked'], ['done', 'Done'], ['dropped', 'Dropped']];
    stats.replaceChildren();
    labels.forEach(([key, label]) => {
      const count = key === 'total' ? tasks.length : tasks.filter(task => task.status === key).length;
      const card = node('button', 'stat' + (key === 'total' ? '' : ' status-chip'));
      card.type = 'button';
      card.dataset.status = key;
      if (key === 'total') {
        const all = STATUSES.every(s => shownStatuses.has(s));
        card.title = all ? 'Show the default statuses (open and blocked)' : 'Show all statuses';
      } else {
        card.setAttribute('aria-pressed', shownStatuses.has(key) ? 'true' : 'false');
        card.title = (shownStatuses.has(key) ? 'Hide ' : 'Show ') + label.toLowerCase() + ' tasks';
      }
      card.append(node('span', 'stat-value', count), node('span', 'stat-label', label));
      card.addEventListener('click', () => toggleStatus(key));
      stats.append(card);
    });
  }

  const openTodoGroups = new Map();

  function renderTodo(groups) {
    const section = document.getElementById('todo');
    const container = document.getElementById('todo-groups');
    container.replaceChildren();
    const total = groups.reduce((sum, group) => sum + group.items.length, 0);
    section.hidden = !total;
    document.getElementById('todo-count').textContent = total + (total === 1 ? ' task waits' : ' tasks wait') + ' for you';
    groups.forEach(group => {
      const details = node('details', 'todo-group');
      details.open = openTodoGroups.has(group.key) ? openTodoGroups.get(group.key) : group.key !== 'later';
      details.addEventListener('toggle', () => openTodoGroups.set(group.key, details.open));
      const summary = node('summary');
      summary.append(node('span', 'disclosure', '▶'), node('span', '', group.title),
        node('span', 'area-total', String(group.items.length)));
      const list = node('ul', 'todo-items');
      group.items.forEach(item => {
        const li = node('li', 'todo-item');
        const line = node('div', 'todo-action');
        const link = node('a', '', '#' + item.num);
        link.href = '#task-' + item.num;
        link.title = 'Open the task with its full status, notes and messages';
        link.addEventListener('click', event => { event.preventDefault(); showTask(item.num); });
        line.append(link, document.createTextNode(' ' + item.action));
        li.title = item.reason;
        li.append(line, node('div', 'todo-meta', (item.title || '') + ' · ' + (item.owner || 'no owner')));
        if (item.command) {
          const row = node('div', 'todo-command');
          const copy = node('button', 'copy', 'Copy');
          copy.type = 'button';
          copy.addEventListener('click', async () => {
            try { await navigator.clipboard.writeText(item.command); copy.textContent = 'Copied'; }
            catch (err) { copy.textContent = 'Select and copy'; }
          });
          row.append(node('code', '', item.command), copy);
          li.append(row);
        }
        list.append(li);
      });
      details.append(summary, list);
      container.append(details);
    });
  }

  function showTask(num) {
    const task = (currentState && Array.isArray(currentState.tasks) ? currentState.tasks : []).find(t => t.num === num);
    if (!task) return;
    if (STATUSES.includes(task.status) && !shownStatuses.has(task.status)) shownStatuses.add(task.status);
    if (search.value && !searchText(task).includes(search.value.trim().toLowerCase())) search.value = '';
    openTasks.add(num);
    openAreas.set(task.area || '', true);
    renderStats(currentState.tasks);
    renderTasks();
    if (window.location.hash !== '#task-' + num) history.replaceState(null, '', window.location.pathname + window.location.search + '#task-' + num);
    const el = document.getElementById('task-' + num);
    if (!el) return;
    el.scrollIntoView({behavior: 'smooth', block: 'start'});
    el.classList.add('flash');
    window.setTimeout(() => el.classList.remove('flash'), 2000);
  }

  function hashTask() {
    const match = /^#task-(\d+)$/.exec(window.location.hash);
    return match ? Number(match[1]) : null;
  }

  function toggleStatus(key) {
    if (key === 'total') {
      const all = STATUSES.every(s => shownStatuses.has(s));
      shownStatuses.clear();
      (all ? DEFAULT_STATUSES : STATUSES).forEach(s => shownStatuses.add(s));
    } else if (shownStatuses.has(key)) {
      shownStatuses.delete(key);
    } else {
      shownStatuses.add(key);
    }
    if (currentState) renderStats(Array.isArray(currentState.tasks) ? currentState.tasks : []);
    renderTasks();
  }

  function statusShown(task) {
    // A status outside the known set is shown only when every known status is shown.
    if (STATUSES.includes(task.status)) return shownStatuses.has(task.status);
    return STATUSES.every(s => shownStatuses.has(s));
  }

  function renderTasks() {
    if (!currentState) return;
    const tasks = Array.isArray(currentState.tasks) ? currentState.tasks : [];
    const query = search.value.trim().toLowerCase();
    const visible = tasks.filter(task => statusShown(task) && (!query || searchText(task).includes(query)));
    const areas = document.getElementById('areas');
    const noMatches = document.getElementById('no-matches');
    areas.replaceChildren();
    document.getElementById('visible-count').textContent = visible.length + ' of ' + tasks.length + ' shown';
    noMatches.hidden = !(tasks.length && !visible.length);
    if (!tasks.length) {
      areas.append(empty('No tasks in this worklog.'));
      return;
    }
    const groups = new Map();
    visible.forEach(task => {
      const area = task.area || '';
      if (!groups.has(area)) groups.set(area, []);
      groups.get(area).push(task);
    });
    [...groups.keys()].sort((a, b) => a.localeCompare(b)).forEach(area => {
      const groupTasks = groups.get(area).sort((a, b) => Number(a.num) - Number(b.num));
      const details = node('details', 'area');
      details.open = openAreas.has(area) ? openAreas.get(area) :
        area === [...groups.keys()].sort((a, b) => a.localeCompare(b))[0];
      details.addEventListener('toggle', () => openAreas.set(area, details.open));
      const summary = node('summary');
      summary.append(
        node('span', 'disclosure', '▶'),
        node('span', 'area-name', area || 'Unassigned'),
        node('span', 'area-total', groupTasks.length + (groupTasks.length === 1 ? ' task' : ' tasks'))
      );
      const list = node('div', 'area-tasks');
      groupTasks.forEach(task => list.append(taskNode(task)));
      details.append(summary, list);
      areas.append(details);
    });
  }

  function renderWorkers(workers) {
    const container = document.getElementById('workers');
    container.replaceChildren();
    document.getElementById('worker-count').textContent = String(workers.length);
    if (!workers.length) {
      container.append(empty('No workers registered.'));
      return;
    }
    workers.forEach(worker => {
      const item = node('div', 'worker');
      item.append(node('div', 'worker-name', worker.name));
      item.append(node('div', 'item-meta', (worker.machine || 'Unknown machine') + ' · Seen ' + formatTime(worker.seen)));
      if (worker.desc) item.append(node('p', 'worker-desc', worker.desc));
      if (worker.areas && worker.areas.length) {
        const tags = node('div', 'tags');
        worker.areas.forEach(area => tags.append(node('span', 'tag', area)));
        item.append(tags);
      }
      container.append(item);
    });
  }

  function renderLeases(leases) {
    const container = document.getElementById('leases');
    container.replaceChildren();
    document.getElementById('lease-count').textContent = String(leases.length);
    if (!leases.length) {
      container.append(empty('No active or recorded leases.'));
      return;
    }
    leases.forEach(lease => {
      const item = node('div', 'lease');
      item.append(node('div', 'lease-name', lease.resource));
      item.append(node('div', 'item-meta', 'Owner: ' + (lease.owner || '—')));
      if (lease.why) item.append(node('p', 'lease-why', lease.why));
      item.append(node('div', 'item-meta', 'Since ' + formatTime(lease.since) + ' · Until ' + formatTime(lease.expires)));
      container.append(item);
    });
  }

  function renderHistory(history) {
    const container = document.getElementById('history');
    container.replaceChildren();
    document.getElementById('history-count').textContent = String(history.length);
    if (!history.length) {
      container.append(empty('No recent activity.'));
      return;
    }
    history.slice().reverse().forEach(entry => {
      const item = node('div', 'history-item');
      item.append(node('p', 'history-text', entry.text));
      item.append(node('div', 'item-meta', (entry.by || 'Unknown worker') + ' · ' + formatTime(entry.at)));
      container.append(item);
    });
  }

  function render(data) {
    currentState = data;
    document.getElementById('machine').textContent = 'Machine: ' + (data.machine || 'unknown');
    document.getElementById('generated').textContent = 'Updated ' + formatTime(data.generated_at);
    renderTodo(Array.isArray(data.todo) ? data.todo : []);
    renderStats(Array.isArray(data.tasks) ? data.tasks : []);
    renderTasks();
    if (!hashHandled) {
      hashHandled = true;
      const num = hashTask();
      if (num !== null) showTask(num);
    }
    renderWorkers(Array.isArray(data.workers) ? data.workers : []);
    renderLeases(Array.isArray(data.leases) ? data.leases : []);
    renderHistory(Array.isArray(data.history) ? data.history : []);
  }

  async function refresh() {
    if (inFlight) return;
    inFlight = true;
    refreshButton.disabled = true;
    errorBox.hidden = true;
    try {
      const response = await fetch('state' + window.location.search, {headers: {'Accept': 'application/json'}});
      if (!response.ok) throw new Error('State request returned HTTP ' + response.status);
      const data = await response.json();
      render(data);
    } catch (error) {
      errorBox.textContent = 'Refresh failed: ' + (error instanceof Error ? error.message : String(error));
      errorBox.hidden = false;
    } finally {
      loading.hidden = true;
      refreshButton.disabled = false;
      inFlight = false;
    }
  }

  function schedule() {
    if (timer !== null) window.clearInterval(timer);
    timer = autoRefresh.checked ? window.setInterval(refresh, 10000) : null;
  }

  search.addEventListener('input', renderTasks);
  window.addEventListener('hashchange', () => { const num = hashTask(); if (num !== null) showTask(num); });
  refreshButton.addEventListener('click', refresh);
  autoRefresh.addEventListener('change', schedule);
  schedule();
  refresh();
})();
</script>
</body>
</html>
"""

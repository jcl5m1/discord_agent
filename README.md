# discord_agent

Chat with coding-agent instances (Claude Code, Kimi Code and Muse, extensible to others) from
Discord — designed for driving agents from your phone.

- **One agent instance = one Discord channel.** Create instances with `/new`; each
  channel is a full chat session with an agent.
- **One Discord message = one agent turn.** The agent's reply streams into a single
  message in the channel, edited in place.
- **Sessions survive restarts.** The harness stores each instance's provider session
  ID on disk. If the harness (or the whole machine) goes down, the next message in
  the channel transparently resumes the agent exactly where it left off.
- **Background work survives the reply.** An agent's subagents and background commands keep
  running after it replies: while the CLI reports background tasks, the harness keeps the turn's
  input open. With its input closed, the Claude CLI exits at the end of its next turn (for example
  the turn that reports a finished subagent) and stops every background task still running.
  While a turn only waits for background tasks it posts no "still working" notes, it is listed
  under `background_turns` (not `turns`) in `data/heartbeat.json`, and `/update` treats it as
  idle (the restart resumes it). A message sent then goes into the session, and the turn takes
  its slot back while the agent answers. The turn gives its concurrency slot back meanwhile, and messages sent to
  the channel go into the running session. Input closes when no background task is left, or after
  `BACKGROUND_WAIT_MAX_HOURS` (12).
- **Work cut off by a restart resumes by itself.** A turn lasts as long as the agent's
  process, including background subagents after its reply. Each running turn is recorded
  in `data/interrupted_turns.json` until it ends. After a restart, each channel whose turn
  was cut off gets a resume turn: it says what was stopped and asks the agent to check
  its work and continue. The cut-off message is not run again as new. A turn cut off by
  more than 2 restarts in a row is not resumed again. Turn this off with `AUTO_RESUME=false`.
  To restart from a terminal: `.venv/bin/python -m harness.resume restart` (it also records
  the turns of a harness that is older than this feature).

## Install in one line

On a Linux machine (Ubuntu/Debian or anything with systemd), run:

```bash
curl -fsSL https://raw.githubusercontent.com/jcl5m1/discord_agent/main/setup.sh | bash
```

On Windows, run this in PowerShell:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -Command "iex (irm https://raw.githubusercontent.com/jcl5m1/discord_agent/main/setup.ps1)"
```

That single command does the full setup, asking questions as it goes:

1. **Prerequisites:** installs Git if missing and provides Python 3.12 through
   an existing launcher or `uv` (Linux also installs `curl` via apt, needs sudo).
2. **Code:** clones this repo to `~/discord_agent` (`--dir <path>` to change),
   or pulls if it's already there.
3. **Dependencies:** builds the venv and installs headless Chromium for HTML
   rendering.
4. **Agent CLIs:** for Claude Code, Kimi Code and Muse, offers to install each
   one with its official installer, checks whether it's logged in, and if not
   runs that CLI's own login (`claude auth login`, `kimi login`, `muse login`).
   Your agent credentials stay with each CLI and never pass through the
   harness.
5. **Discord:** walks you through creating the bot in the developer portal (the
   one manual step), then asks for the bot token, server ID and category ID.
6. **Options:** default agent and working directory, plus optional WhatsApp
   numbers and public URL. Writes `.env` (mode 600).
7. **Checks:** verifies the token, server, category and Message Content intent
   against Discord. If the bot isn't in your server yet, it prints the invite
   link with the right permissions.
8. **Services:** on Linux, installs and starts the bot, watchdog and uptime
   systemd user services with lingering; on Windows, installs current-user
   logon tasks instead.

Rerunning it is safe: anything already installed or logged in is skipped,
current settings are the defaults, and the old `.env` is kept as `.env.bak`.
Use `--no-service` to skip systemd and start with `./run.sh` instead. To pass
options through the pipe: `curl -fsSL … | bash -s -- --dir ~/agents --no-service`.

## How it works

```
your phone (Discord)  ⇄  harness (this repo, discord.py bot)
                                │  one subprocess per active model
                                ▼
                        claude CLI (`claude -p`) or kimi CLI (`kimi -p`)
                                │  sessions persisted by the CLI itself
                                ▼
                        ~/.claude/projects/  ·  ~/.kimi-code/sessions/
```

Every message in a managed channel starts with one print-mode invocation — e.g.
`claude --model claude-opus-5-5 -p "<message>"`, plus `--resume <id>` once a session
exists (`kimi --session <id> -p ...` for Kimi). A primary usage limit can start a second
invocation on the secondary. The CLI's own session store makes instances durable. The
harness remembers each provider's session ID in `data/instances.json`.

> **Note:** both providers run tools without asking — `claude -p` with
> `--permission-mode bypassPermissions` (configurable via `CLAUDE_PERMISSION_MODE`),
> `kimi -p` under Kimi's `auto` policy. Only point instances at working directories
> you trust.

### Signing in: subscriptions, not API keys

The agents run on your subscription logins (Claude Pro/Max, Kimi Code, Muse).
Provider API keys switch the CLIs to per-token billing, so the harness ignores
them: `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `META_API_KEY`,
`MOONSHOT_API_KEY` and `KIMI_API_KEY` are removed from its environment and every
agent's, and startup logs which ones it ignored. Set `ALLOW_AGENT_API_KEYS=true`
only if you want API billing.

Sign in from Discord with `/login` (server admins; replies only you can see):

- `/login` shows each CLI's status: subscription, API billing, or not signed in.
- `/login provider:kimi` or `provider:muse` posts a sign-in link and a code to
  confirm. Approve it in any browser, such as your phone's, and the bot confirms
  when the CLI has finished.
- `/login provider:claude` posts a sign-in link. After you approve, the page shows
  a code: send it with `/login provider:claude code:<the code>`.
- `again:True` signs in again when a CLI is already signed in.

The harness never stores credentials: each CLI keeps its own login, exactly as if
you'd signed in at the machine. The setup scripts also drop any of those API keys
from `.env` (and its backup), and on Windows offer to delete them from your user
environment variables.

## Setup

The [one-line install](#install-in-one-line) does all of this automatically;
the steps below are the manual equivalent. Step 1, creating the bot in the
developer portal, is needed either way.

### 1. Create the Discord bot (one-time)

1. Go to <https://discord.com/developers/applications> → **New Application** → give
   it a name (e.g. `agent-bot`) → **Create**.
2. Open **Bot** in the left panel → **Reset Token** → **Copy** the token
   (you only see it once — paste it into `.env` below).
3. Still on the Bot page, scroll to **Privileged Gateway Intents** and enable
   **Message Content Intent**.
4. Open **OAuth2 → URL Generator** in the left panel:
   - Scopes: check `bot` **and** `applications.commands`
   - Bot permissions: check **Manage Channels**, **View Channels**, **Send Messages**,
     **Manage Messages** (needed to edit the streaming reply), **Read Message History**,
     **Add Reactions** (✅/❌ on turns), **Attach Files** and **Embed Links** (rendered
     images, HTML source), **Change Nickname** (optional). Add **Manage Roles** too if you want `/stop` to
     lock channels (without it `/stop` still retains the session, it just can't lock).
   - Copy the generated URL at the bottom, open it in a browser, and invite the bot
     to your server.

### 2. Get your server and category IDs

1. In Discord: **User Settings → Advanced → Developer Mode** → enable.
2. Right-click your **server** → **Copy Server ID** → that's `DISCORD_GUILD_ID`.
3. Create a **category** on your server (e.g. `agents`) where the agent channels will
   live. Right-click it → **Copy Channel ID** → that's `DISCORD_CATEGORY_ID`.

### 3. Configure and run

```bash
cp .env.example .env   # then fill in the three Discord values
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# one-time: headless Chromium for rendering rich HTML output (kept repo-local)
PLAYWRIGHT_BROWSERS_PATH=.pw-browsers .venv/bin/playwright install chromium
./run.sh
```

The bot syncs its slash commands on startup and logs in. Leave it running.

### Windows

The one-line PowerShell installer above installs any missing prerequisites,
clones to `~\discord_agent`, builds the venv and installs Chromium, and offers
the Claude/Kimi/Muse installers and logins. On a new install it can import
non-secret defaults from another machine over SSH; the bot token and category
ID are always excluded and Linux paths are translated for Windows.

The wizard securely asks for the Discord token, pauses while you invite the bot,
validates the server/category/intent, and checks subscription logins while
keeping provider API keys from taking precedence. It can run a real provider
turn, then install or cleanly restart the current-user logon tasks and verify
fresh bot and uptime heartbeats. Existing settings are the defaults when rerunning it;
`.env.bak` keeps the previous configuration. The token is never placed on the
command line.

From an existing checkout, run `powershell -ExecutionPolicy Bypass -File .\setup.ps1`.
Options are `-Directory <path>`, `-NoService` (skip logon tasks), `-ConfigureOnly`
(skip code, dependencies and CLI installation), `-SkipChecks` (skip Discord API
validation), and `-SkipProviderTest` (skip the real provider turn). Use Task
Scheduler to stop or disable the tasks later.

For a manual setup, edit `windows.env.example`, then run `.\save-token.ps1` to
create `.env` and securely save the token. An optional `windows.config.local`
(Git-ignored) overrides the template's machine-specific defaults. `.\run.ps1`
starts the bot and watchdog; `.\install-windows-tasks.ps1 -Start` installs and
starts the logon tasks. The manual dependency commands are:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
$env:PLAYWRIGHT_BROWSERS_PATH = ".pw-browsers"
.venv\Scripts\python -m playwright install chromium
```

### 4. Use it (from any Discord client, including your phone)

- `/new name:my-project workdir:/home/you/code/my-project prompt:give me a summary of this repo`
  creates `#my-project` under the agents category and optionally starts it with a prompt.
  `workdir` browses folders as you type: it starts with your home folder (and
  folders your agents already use), picking a folder ending in `/` lists its
  subfolders next, and "use this folder" picks the one you're in. Typing filters
  the current level; a name that doesn't exist yet is created.
- Just type in the channel — every message is a turn. Watch the 🤔 message update as
  the agent works. ✅ = turn finished, ❌ = error (check harness logs).
- Messages you send while the agent is working go **straight into the running
  turn** for Claude: it reads them at its next step, the way the CLI's own queue
  works. ⏩ = sent into the turn, ✅ = the agent has read it; the reply continues
  in the same message. For kimi and muse, or with `STEER_RUNNING_TURNS=false`,
  they wait for the turn to finish.
- A message to an idle agent waits `SEND_DELAY` seconds (default 5, typing
  indicator showing) before its turn starts, so deleting it (say, sent to the wrong
  channel) or fixing a typo right away means the agent never sees it, or sees only
  the fixed text. Deleting a message whose turn has already started stops that turn,
  and the agent's next turn begins with a note to disregard it and anything it did
  for it.
- Edit or delete a message before the agent has it and the change sticks: queued
  messages are updated or dropped, and a message sent into Claude's running turn is
  held for `STEER_HOLD` seconds (default 10) before Claude gets it, so a quick edit
  or delete means Claude only ever sees the final text, or nothing. After that, an
  edit or delete during the turn is passed on as a correction ("the user edited
  their message…") or a retraction ("…disregard it"); after the turn, an edit
  becomes a follow-up turn.
- If the agent's CLI crashes before replying, the bot says so, restarts the session
  and resends your message; if it crashes again, your message goes to a fresh
  session (`/session` can switch back to the old one). A turn also ends as soon as
  the CLI exits, even if a program it started in the background still holds its
  output open (that used to leave a session silently "running" forever).
- A provider with no events for `AGENT_STALL_AFTER` seconds (default 1800) is stopped
  with its process tree. The harness continues once on the same session. A second stall
  requests help without another retry. Set it to 0 to disable this recovery. Background-only
  waits after a reply do not use this timeout.
- A message that has to wait behind a running turn gets one status note per turn:
  how long the agent has been at it, when it last produced output, and how to stop
  it. The same note appears if a message you send into a running turn finds it
  silent for 10 minutes or more.
- While the agent works, its "working…" message has a **⏹️ Stop** button: the same
  graceful stop as sending `stop`. It goes away when the turn ends.
- **Start a message with `!` to interrupt and redirect**, e.g. `! use postgres
  instead`: the running turn stops gracefully, messages queued behind it are
  dropped, and your message runs next on the same session (so the agent keeps the
  context of what it was doing). Works for every agent, in Discord or WhatsApp; with
  nothing running, the message is sent as typed. Sending just `stop` only stops.
- `/interrupt` does the same as a command (`message:` for the new instructions,
  `keep_queue:True` to keep queued messages). Stopping is graceful for every agent:
  Claude gets its own interrupt (like pressing Esc), kimi and muse get Ctrl-C, which
  cancels their running command and saves the session within a couple of seconds.
  Only if an agent hasn't stopped within `INTERRUPT_GRACE` seconds is it killed,
  with every command it started. Background servers started detached
  (`setsid nohup … &`) keep running.
- Editing or deleting a message works the way you'd expect. A message still
  waiting its turn (✏️ = edit received) is sent with its latest text, or dropped
  if you delete it. If the agent already has the message, an edit queues a
  follow-up turn that shows the agent both versions. Deleting a message whose turn
  is running doesn't stop it. Edits to messages from before the harness last
  restarted are ignored.
- `/new name:x provider:claude model:sonnet` picks the primary agent CLI and model.
  New instances default to Claude Opus 5.5. `provider` defaults to
  `DEFAULT_PROVIDER`; Claude's model defaults to `CLAUDE_MODEL` (`claude-opus-5-5`).
  Aliases `opus`, `sonnet`, `haiku`, `fable` are autocompleted; any full model ID works too.
  `/model model:claude:fable` selects Claude Fable 5.2 (`claude-fable-5-2`).
  Fable may consume subscription usage credits. This non-interactive harness does not
  show Claude Code's interactive credit-consent prompt.
- `/model` (inside an agent channel) shows the primary and secondary models.
  The `model` autocomplete lists every provider's models as `provider:alias`
  (e.g. `claude:sonnet`, `kimi:k3`, `muse:spark-1.3`); picking one from another
  provider switches provider too and starts a fresh session. Same-provider
  changes keep the session.
- `/model secondary:kimi:k3` selects the secondary model. Use `secondary:off` to
  disable fallback for that channel, or `secondary:on` to restore the configured default.
  `/new` accepts the same `secondary` argument. The default secondary is Kimi with its
  configured default model (`SECONDARY_PROVIDER=kimi`, `SECONDARY_MODEL` empty).
- When Claude reports a usage, token, or credit limit, the request moves to
  the secondary at once. Corrections sent during that turn move with it. New turns stay
  on the secondary until one minute after Claude's expected reset. If the reset is unknown,
  the harness tries Claude again after 30 minutes. Each provider keeps its own session.
  After the reset, Claude gets a bounded handoff of secondary requests, outcomes, and full
  or partial replies. It then continues its original session. Set `AUTO_FALLBACK=false`
  or empty `SECONDARY_PROVIDER` to restore the old wait-and-resume behavior everywhere.
- `/session` (inside an agent channel) shows the channel's session. Pick one
  from the `session` autocomplete (the CLI's own sessions, newest first, with
  their title and folder) to continue it here — e.g. a Kimi or Claude session
  you started in a terminal. The instance's workdir becomes that session's
  folder. Close the session anywhere else first: Kimi refuses a session another
  `kimi` process has open. Agents are told not to resume sessions themselves.
- `/list` shows active instances with their provider, model and session IDs;
  stopped instances are listed separately at the bottom.
- `/stop name:my-project` stops the instance (no replies, hidden from the active
  list) and locks the channel; the session is kept.
- `/resume name:my-project` brings a stopped instance back — unlocking its
  channel, or recreating the channel if it was deleted in Discord while the
  instance is still tracked. The session resumes on the next message.
- `/delete name:my-project` stops the instance (if active) and removes it for
  good — channel deleted, instance forgotten.
- `/new name:my-project` again, for a name that already exists, asks whether to
  **Resume** its conversation in the new channel or **Start fresh** (a new session,
  using `workdir` if you give one). Resume waits until any turn still running in the
  old channel finishes, because two turns can't share one session.
- If a turn fails because its session is open in another process (kimi: "in use by
  another Kimi Code CLI instance"), the error offers **Start fresh & retry** and
  **Retry**.
- `/usage` shows plan usage / remaining quota as each CLI reports it
  (`claude -p /usage`, `muse usage`, and kimi's account usage via a short-lived
  local `kimi web` server). Defaults to the channel's provider, or all providers
  outside agent channels; `provider:all` asks every CLI.
- `/update` (server admins) runs `git pull --ff-only`, reinstalls
  `requirements.txt` if it changed, and restarts the harness — the systemd unit
  if it runs under one, otherwise an in-place re-exec. It refuses while agent
  turns are running unless `force:True`, skips the restart when already up to
  date, and posts a confirmation once the new code is back online.
  Before restarting it syncs the systemd unit files from `harness/service.py`
  (see below), so unit changes ship with the pull.
- `/update all:True` updates every machine's harness from one command. It posts an
  `update` event in the worklog. Each bot that runs this code does the same steps as
  `/update` and posts an `updated` event with its result (old → new commit, or the
  error). A machine with agent turns running skips the restart unless `force:True`.
  The machine that asked waits up to 2 minutes for the others, posts one result line
  per machine in the channel, then updates itself. After its restart it posts the
  results again, with late ones, and lists machines with no answer. A machine that
  was offline runs a request from the last 6 hours when it comes back. Each machine
  runs a request once (`data/update_handled.json`). On Windows the restart runs
  `install-windows-tasks.ps1 -Start` outside the harness process tree. A machine
  must already run this code to answer, so run `/update` once on each machine first.

### Running as a systemd user service

When the harness runs as the user unit `discord-agent.service`, it manages its
own unit files: on every startup (and on `/update`) `harness/service.py`
renders `discord-agent.service` and `discord-agent-watchdog.service` for this
machine (repo path, venv, a PATH that includes `~/.local/bin` and
`~/.kimi-code/bin`) into `~/.config/systemd/user/`, rewrites them only if they
differ, and runs `systemctl --user daemon-reload`. A `git pull` plus restart is
therefore all a machine needs. Run `python -m harness.service` to sync by hand.
Started any other way (e.g. `run.sh`), the harness leaves unit files alone.

### Rich output (tables, dashboards, styled HTML)

Discord can't render HTML in messages. If the agent's reply contains a fenced
` ```html ` block, the harness screenshots it with headless Chromium and posts
the PNG inline, alongside the raw `.html` as an attachment. To use it, ask for
what you want in an HTML code block, e.g.:

> make me a styled HTML table of the files in this repo with their sizes, in a ```html block

The image renders inline on mobile; tap the attachment for the source.

### Markdown in Discord

Discord shows only part of Markdown, so replies are adapted before posting
(`harness/discord_markdown.py`). It has no tables, and its code blocks wrap long
lines instead of scrolling, which breaks a text table's alignment on a phone. So:

- tables that fit a phone (up to 32 characters wide) become an aligned code block;
- wider tables of short values become an **image** of the table (headless Chromium,
  in Discord's dark colours), attached under the part of the reply that mentions
  it, with a "📊 table 1, shown below" line where the table was; WhatsApp gets the
  images too. If rendering fails, the reply falls back to text;
- tables of long sentences become a list, one entry per row, which reads better on
  a phone than a shrunken image;
- tables an agent draws itself inside a code block (a dashed rule under the
  header) are detected and treated the same way when they're too wide for a phone.

Headings below `###` become bold lines, and `---` dividers are dropped. Replies over
Discord's 2000-character limit are split at line breaks, and a code block cut by a
split is closed and reopened so both halves render.

### WhatsApp mirror (optional)

Every instance can also get a WhatsApp group, so you can talk to the same
session from either app. Both sides stay in sync: what you type in Discord is
echoed into the group, what you type in the group is echoed into the channel
(and runs as a turn), and every agent reply, progress note, rendered image and
live-page link goes to both.

It needs a **separate WhatsApp account** for the bot (a second number; the
WhatsApp Business app on a spare SIM or eSIM works). The harness links to it as
a companion device, the way WhatsApp Web does, using
[neonize](https://github.com/krypton-byte/neonize) (whatsmeow). That is an
unofficial client, so keep it to a dedicated number you could afford to lose.

1. Set `WHATSAPP_OWNER` in `.env` to *your own* number with country code, then
   restart the harness.
2. Run `/whatsapp` in Discord and scan the QR with the **bot** phone (WhatsApp →
   Linked devices → Link a device), or run `/whatsapp phone:<bot number>` to get
   a code for "Link with phone number instead". With `WHATSAPP_BOT_NUMBER` set
   in `.env`, plain `/whatsapp` gives that code. The link is stored in
   `data/whatsapp.db` and survives restarts.
3. Once linked, the bot creates a group named `<instance>-bot` for each active
   instance with you in it. New ones get theirs on `/new` or `/resume`. If your
   privacy settings block being added to groups, the bot sends you an invite
   link instead.

Only messages from `WHATSAPP_OWNER` are acted on; anyone else in a group is
ignored. Only text is forwarded, so captions count but bare media doesn't.
`/stop` and `/resume` post a note in the group, and `/delete` leaves it.
Slash commands stay Discord-only.

### Several bots on one server

Each machine runs its own bot with its own category. A bot only takes slash
commands in its category, its instance channels and `DISCORD_SHARED_CHANNELS`
(default `general`). Elsewhere it refuses with a short reply only you can see, so
picking the wrong bot's command can't run anything on the wrong machine.

Discord still lists every bot's commands in the picker. Its channel settings under
Server Settings → Integrations don't hide them from a server owner, so the picker
can't be cleaned up for an admin account. Each bot needs this code to be guarded.

### Logs and `/logs`

Each process writes `data/logs/<bot|watchdog|uptime>.log` (rotated at 5 MB, 3 old
files kept), so logs survive restarts on every OS, including Windows, where the
logon tasks have no console to read afterwards. `/logs` (admins) posts this
machine's recent lines, plus systemd's journal on Linux, as a file in the channel,
with a summary of the bot's instances and sessions at the top. Anything that looks
like a token or key is replaced with `[redacted]`. To get another machine's logs to
the agent on this one, run that bot's `/logs` and tell the agent which channel it's
in: the agent can download the file with its own bot's access.

### Shared worklog (`wl`, `/worklog`)

Agents on every machine coordinate through one shared worklog: a task list with
claims, progress check-ins, notes, resource leases and messages. The harness
provides it, so sessions don't each build their own.

- **Dashboard:** `/worklog` opens a token-protected web dashboard. It groups every
  project by area. Expand a project or task to see its latest status, details,
  dependencies, check-in/note history, and task thread. You can reply to the assigned
  agent from the task. The reply persists in worklog state and immediately notifies
  that agent, or drop the task (it asks for an optional reason and tells the owner to
  stop). A "To do for you" panel at the top groups the blocked tasks that wait for the
  user by action (go-ahead, run a command, phone test, accounts, decide, parked); each
  item links to its task (`#task-<n>`), and a command in the block message gets a Copy
  button. When a task asks only for the user's OK, the item and the task's Reply row get
  **Send OK** ("OK, go ahead."); when it asks the user to run a command, the item gets
  **Done**. Each area has **Area details**: web links (shown as chips in the area's
  title row) and notes, shared through the worklog. Agents set links with
  `wl area <name> --link URL --label NAME`; the user adds notes and links in the page.
  The status chips filter the list (default: open and blocked; tap to toggle,
  tap Total for all). Search and worker/lease summaries are included. **Live updates:**
  the page listens on a server-sent event stream (`events`) and, after each worklog
  change, gets the new state and changes only the parts that changed. It never moves
  the text box that has the focus, and it keeps the view in place. A reply or note
  draft is saved in the browser every 5 seconds when it changed ("Draft saved at
  YYYY-MM-DD HH:MM:SS") and comes back after a reload. **Links:** web addresses,
  `[label](target)` links and file names (.md, images, videos, PDF, logs) in work items
  are links. A file opens through `file?path=..&task=N`: the dashboard finds it in the
  owner's or creator's folder (or another agent's folder on this machine), sends a
  published .md to its Google Doc (`DASHBOARD_GDOC_REGISTRIES`), and sends other files
  read-only and sandboxed. Only files under the home folder or `DASHBOARD_FILE_ROOTS`
  open; dot-folders, secret-like names and other suffixes do not. It uses the public proxy when configured, otherwise a loopback URL. Use
  `/worklog command:"status"` or another explicit command for text output.
- **Transport:** a `#worklog` channel (created on first start). Every change is one
  message there: a readable line, plus the event as small JSON. Each bot keeps the
  events in `data/worklog.db` and applies them in message-id order, which Discord
  makes the same on every machine; an event arriving late triggers a replay. A
  change is confirmed about 2.5 s after posting, so a competing claim from another
  machine wins if it reached Discord first; the loser's line is marked "❌ refused".
  The channel is hidden from people and open to every bot's role (each bot sets
  this at startup and when another bot joins). Discord still shows every channel
  to the server owner and admins, so mute it: right-click `#worklog` → Mute Channel,
  and turn on Hide Muted Channels. Use `/worklog` to read it instead.
- **Agents** run `wl …`: the harness puts `bin/wl` on their PATH and sets
  `WORKLOG_WORKER` (`<instance>@<machine>`), so they never see the bot token. The
  session brief explains the rules; existing sessions are told once, on their next
  message. Terminal sessions on the same machine can use `bin/wl --as <name>` too.
- **Check-ins:** while holding a claim, an agent must run
  `wl checkin <id> "<up to 10 words of progress>"` at least every 30 minutes. Each
  check-in updates the dashboard; it is not reposted to the agent's Discord channel.
  After 25 minutes the harness reminds the agent, and at 30 minutes it releases the claim.
  If the provider process is still active, the claim stays assigned until turn cleanup
  to prevent duplicate work.
  A terminal session's lapsed claim is released too, by the bot that posted its claim or
  last check-in (or by any bot an hour later, if that one is down), and the session gets
  a message saying so. `wl status` shows a worker as stale after 30 minutes without activity,
  or while it holds a lapsed claim.
- **Open work is pulled, not left waiting.** A task is *ready* when it is open
  and every task it waits on (`--after`, `wl depends N --on 42,43`) is done or
  dropped. `wl next` shows the best ready task. Each minute, the harness looks for
  its agents that are idle for 10 minutes, hold no claim, and had a user message
  in the last 2 days (`WORKLOG_OFFER_ACTIVE_DAYS`). It offers each one the best
  ready task, preferring a task whose area matches the agent's name or folder: at
  most one offer per agent per hour, and one agent per task for 30 minutes. The
  agent is offered only tasks in its own areas, so other projects' work never
  enters its context: its folder's name, its name, the areas it adds with
  `wl areas a,b,c`, and `WORKLOG_AREA_ALIASES` (default `discord_agent=harness`).
  An area matches an area that extends it (`placeframe` ~ `placeframe-insta360`).
  A task with no area is offered to nobody, so give each task an `--area`. The
  agent claims it, or says why not with `wl pass N "<why>"`; a pass hides the task
  from that agent and its subagents. `WORKLOG_OFFERS=0` turns offers off.
- **Mail:** `wl send <worker|all> "…"`. The harness delivers a direct message to its
  agent, into the running turn or as a new turn (at most 12 per agent per hour, so
  two agents can't ping-pong forever); messages to `all` wait in inboxes.
- **Humans:** `/worklog` shows the status, or runs any `wl` command
  (`/worklog command:"show 8"`).
- `wl import-legacy PATH [--map old=new …]` copies a per-project worklog database
  (from the earlier `ops/worklog.py`) into an empty shared worklog.

Settings: `WORKLOG_ENABLED` (default true), `WORKLOG_CHANNEL` (default `worklog`),
`WORKLOG_PORT` (default 8091, the local port `wl` talks to).

### Uptime log (`/uptime`)

A third user unit, `discord-agent-uptime.service`, tracks how often the
internet drops and how long the machine is down. It runs independently of the
bot, so it keeps logging while the harness is restarting or crashed. Every 10 s
(`UPTIME_INTERVAL`) it TCP-connects to 1.1.1.1, 8.8.8.8 and 9.9.9.9
(`UPTIME_TARGETS`); the internet counts as up if any answers, and two failed
checks in a row open an outage, dated from the first failure.

Only state changes go to `data/uptime.jsonl`: internet down/up, monitor
start/stop (noting when the system was shutting down), reboots, and gaps such as
suspend. A tiny `data/uptime_state.json` is overwritten every check with a
"last alive" time. After a reboot (a new kernel boot id) the system counts as
down from that time, or from the clean-shutdown stop, until the new boot.
On first run, earlier reboots are backfilled from the journal.

`/uptime` shows current state, availability with outage count and total
downtime for 24h / 7d / 30d / all time, and the most recent outages. Internet
downtime only counts while the machine was up and monitored.

### Process recorder (memory and CPU)

The uptime monitor also records which processes use the memory and the CPU
(`harness/memmon.py`, Linux; `MEM_MONITOR=0` turns it off). Each minute it
appends a line to `data/mem/<month>.csv`: available memory, swap use, memory
pressure (PSI), the top 6 processes by memory and the top 6 by CPU, with their
command lines. When less than 10% of RAM is available, it records every 10 s,
so the minutes before an out-of-memory freeze are kept (synced to disk).
`/uptime` shows the current memory, the top processes, and `mem_low` events
(under 10% available) from the last 30 days.

### Crash prevention (system health)

Hard crashes here came from memory: programs fill the RAM and the swap, the
machine thrashes, and it freezes. `harness/health.py` watches for it:

- **Levels.** `warn`: under `HEALTH_WARN_PCT` (20%) of RAM available, swap 80%
  used with under 30% available, or memory pressure (PSI some avg10) at 20%.
  `critical`: under `HEALTH_CRIT_PCT` (6%) available, or PSI full avg10 at 30%.
  The process recorder checks every 10 s (every 2 s when not ok) and writes
  `data/health.json`.
- **The health agent acts at `warn`.** The bot tells the health agent
  (`HEALTH_AGENT`; default: the instance that works in the harness repo) to take
  preventive measures: find what grows, ask its owner to pause it (`wl send`),
  stop or restart jobs that can restart, then prevent a repeat. It repeats the
  order every 30 min (10 min at `critical`) while the level stays up.
- **The monitor acts at `critical`.** It stops the biggest process (at least
  1 GB) that is safe to stop: SIGTERM, then SIGKILL after 15 s. A process in a
  Docker container gets `docker restart`. It never stops the agents, the harness,
  the desktop, sshd, databases or container daemons. Each action is a
  `health_action` event. `HEALTH_ACTIONS=0` turns this off.
- **The watchdog alerts** in Discord when the level rises.

For the kernel side, install `earlyoom`, and keep enough swap (see the
README's setup notes, or ask the harness agent for the commands).

### Disk space

A full main disk stops everything quietly: agents can't write, the bot can't
log or save its heartbeat, and the logs stop. So:

- The **watchdog** checks the root disk and the harness data's disk each minute
  (`harness/disk.py`; more with `DISK_PATHS`). It alerts in Discord when a disk
  rises to warn (`DISK_WARN_PCT`, 90% used), critical (`DISK_CRIT_PCT`, 95%) or
  full (under `DISK_FULL_MB`, 500 MB free), with the biggest folders in the home
  folder, and says when it is back under `DISK_OK_PCT` (85%). Its "harness is
  down" alert names a full disk as the likely cause.
- The **bot** posts "disk full" in Discord (at most every 30 min) when it can't
  save its heartbeat because the disk is full, instead of going silent.
- The **uptime monitor** keeps running on a full disk: events wait in memory
  and are written when there is space again; GPU and memory samples are skipped.

### Disk care (keeping the main disk from filling)

`harness/diskcare.py` runs in the uptime monitor and checks the home folder's
disk every 10 minutes (at once when its level rises):

- **At 85% used** (`DISK_CLEAN_PCT`) it cleans what is safe to delete: rebuildable
  caches under `~/.cache` (pip, thumbnails, shader and font caches, the desktop
  search index, …), `uv cache prune`, this user's files in `/tmp` and `/var/tmp`
  untouched for 2 days, dangling Docker images, the Docker build cache, and
  containers stopped for a week. It never touches Docker volumes or model caches.
- **At 90%** (`DISK_OFFLOAD_PCT`), if `DISK_OFFLOAD_TO` is set (a folder on a
  secondary drive), it moves cold data there until the disk is under 80%
  (`DISK_OFFLOAD_TARGET`): folders of at least 1 GB under the home folder where no
  file changed for 14 days, never git work trees, virtual environments, config
  folders or folders a process has open. A moved folder leaves a symlink, so its
  path keeps working.
- **At warn** the bot tells the health agent to find what grew, ask the owners to
  shrink it, and move unused data, with the list of cold folders.

Each action goes to `data/disk_actions.jsonl` and `/uptime`'s event log. By hand:
`python -m harness.diskcare status | scan | clean | offload <folder> | restore <folder>`.

### Heavy jobs (`heavy`)

Agents run heavy work through one shared runner, so jobs don't collide
(`harness/heavy.py`, `bin/heavy`; the onboarding tells them):

```
heavy --class video-decode --mem 8G [--swap 1G] [--cpus 4] [--name ...] -- <command>
heavy status          # who holds which slots; is the gate open
```

- **Slots:** `HEAVY_SLOTS` (default `video-decode=1,gpu=1,ml-cpu=2,android-emulator=1,default=3`)
  machine-wide slots per class; a job waits for a free one and says who holds them.
- **Gate:** a job waits while memory health is warn/critical or the disk is full
  (`--force` skips the gate, not the slot; `--max-wait`, default 4 h, then exit 75).
- **Limits:** the job runs in its own systemd scope `heavy-<class>-<pid>` with
  `MemoryMax`/`MemorySwapMax` (and `CPUQuota` with `--cpus`): over the limit, the
  kernel stops the job, not the machine.
- **Stops with its caller:** stopping heavy stops the scope and the Docker
  containers labelled `heavy.unit=$HEAVY_UNIT`; the uptime monitor sweeps up what a
  hard-killed heavy left.
- **Log:** `data/heavy/jobs.jsonl` (who, class, command, wait, peak memory, exit code).

### GPU watch (automatic reboot for a lost GPU)

A GPU that falls off the PCIe bus (NVIDIA Xid 79) comes back only after a reboot,
and until then programs that touch it hang one by one. `harness/gpuguard.py`
(in the uptime monitor) asks the driver every minute (`nvidia-smi -L`). If the
GPU is gone for 2 minutes, it posts a Discord alert, tells every agent to save
(`wl send all`), waits `GPU_REBOOT_DELAY` (5 min), checks again, and reboots
with `sudo -n /usr/bin/systemctl reboot`. That needs this sudoers rule:

```
echo "$USER ALL=(root) NOPASSWD: /usr/bin/systemctl reboot" | sudo tee /etc/sudoers.d/agent-reboot
sudo chmod 440 /etc/sudoers.d/agent-reboot
```

At most one automatic reboot per `GPU_REBOOT_MIN_GAP` (2 h); past that, or
without the rule, it alerts every hour instead. `GPU_AUTO_REBOOT=0` keeps the
alerts and turns the reboot off. Events: `gpu_lost`, `gpu_back`, `auto_reboot`.

### GPU recorder

On a machine with an NVIDIA GPU, the uptime monitor also records the GPU
(`harness/gpumon.py`; set `GPU_MONITOR=0` to turn it off). Everything goes in
`data/gpu/`:

- `samples/<hour>.csv`: one line per second with power, temperature, clocks,
  utilization, memory, P-state, PCIe link and clock-limit reasons. The monitor
  writes these lines to disk every 5 s, so they survive a crash. It keeps 48 hours.
- `minutes-<month>.csv`: one summary line per minute, kept for good. It includes
  the PCIe error counters, the CPU temperature and the load.
- `processes.log`: the GPU processes and their command lines, when they change.
- `incidents/<time>/`: made when the kernel reports an NVIDIA Xid or a PCIe
  error. It holds the kernel lines, the 15 minutes of samples before the error,
  the GPU processes, the PCIe error counters and `nvidia-smi -q`.

Each error is also a `gpu_error` event in `data/uptime.jsonl`, and `/uptime`
lists them. At startup, the monitor adds Xid errors from the last 30 days of
the journal that it has not logged yet.

## Agent onboarding

Each new session starts with the harness onboarding (`_HARNESS_BRIEF` and
`_WORKLOG_BRIEF` in `harness/bot.py`): output formats, files, sessions, the
shared worklog, and the writing style (ASD-STE100 Simplified Technical English).
When you change it, raise `BRIEF_VERSION`: every existing session then gets the
new version once, on its next turn, marked as updated.

## Reports as Google Docs (`gdoc`)

Agents write every report for the user as Markdown in its repo, and publish it
as a Google Doc with `gdoc publish <file.md>` (`bin/gdoc`, on every agent's
PATH). The onboarding tells them the rules:

- **One Doc per report, for good.** The first publish creates the Doc. Every
  later publish updates the same Doc in place with the Google Docs API: only
  changed text is edited, so the link, the sharing and comments on unchanged
  text persist. Publish never makes a second Doc; it stops with an error if the
  Doc can't be read. `--doc <id or link>` binds an existing Doc to a report.
  `--replace` rewrites the whole body, only when the user asks.
- **Header:** authors (from git), agents (`$WORKLOG_WORKER`, or `--agent` for
  subagents), created and last-updated times.
- **Content:** images are embedded and scaled to the page width; a `<video>`
  becomes a link to a copy on Drive; a relative link to another report's `.md`
  file, or its file name in the text, becomes a link to that report's Doc.
- **Markdown:** CommonMark as on GitHub (markdown-it-py), with tables. A list
  may follow a paragraph line directly; nest items by indenting them to the
  parent's text (2 spaces after `- `, 3 after `1. `). Bullet and numbered lists
  keep their nesting levels and numbering (a bullet nested in a numbered list is
  numbered `a.`, `b.`); code is set in Courier, quotes are indented, header
  rows of tables are bold. `tests/gdoc_render_live.py` publishes a sample of
  every feature to a scratch Doc, checks each block through the Docs API, both
  for a new Doc and for an in-place update, and deletes the Doc (needs the
  setup below and network; not part of the unit tests).
- **Comments:** `gdoc comments <file.md>` lists the open threads with the text
  they are on; `gdoc reply <file.md> <id> "text" --resolve` answers one.
- **Sharing:** each publish shares the Doc with the people in
  `~/.config/discord-agent/gdoc_share` (one `email [role]` per line, role
  `writer`, `commenter` or `reader`; no email is sent).

**Setup** (once per machine; `gdoc check` says what is missing, and the harness
logs it at startup):

1. In the [Google Cloud console](https://console.cloud.google.com/), in a
   project of your own, enable the **Google Drive API** and the **Google Docs
   API**, and make an OAuth client ID (type *Desktop app*).
2. Install [rclone](https://rclone.org/install/) and run `rclone config`: a new
   remote named `gdrive`, type `drive`, with your client ID and secret, scope
   `drive.file` (the tool sees only the files it made).
3. Optional settings in `.env`: `GDOC_REMOTE` (default `gdrive`), `GDOC_FOLDER`
   (Drive folder, default `agent-reports`), `GDOC_REGISTRY` (report → Doc map,
   default `data/gdocs.json`), `GDOC_SHARE_FILE`, `GDOC_WEB_REGISTRY`.

## Shared files (`/files`)

Every instance has a persistent folder for the files its discussion produces:
charts, logs, reports, data. The agent is told about it in its session brief.

```
data/files/<instance-slug>/<path>
  -> {PUBLIC_BASE_URL}/agents/<instance-slug>/<web-token>/files/<path>
```

- Served read-only by the harness proxy (GET/HEAD only), guarded by the same
  per-instance token as live web pages: links are public but unguessable.
- Image links posted in Discord show inline, so the agent can show a chart by
  saving a PNG and linking it, without running a server.
- The folder URL (`.../files/`) is a browsable index, newest first.
- Dotfiles are hidden and not served; paths can't escape the folder.
- `/files [folder]` in a channel lists its files with links.
- Files are kept when an instance is stopped or deleted; remove
  `data/files/<slug>/` by hand to clean up.

## External triggers (CI failures, GitHub Actions, webhooks)

Other systems can start or continue an agent turn by calling a token-protected
endpoint on the same public URL the agent web servers use:

```bash
curl -fsS -X POST "$PUBLIC_BASE_URL/agents/_triggers/<name>" \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"workflow": "CI", "url": "https://github.com/...", "sha": "abc123"}'
```

Each trigger names an **instance** (a template filled from the event, e.g.
`ci-{repo}`, or a fixed existing name) and a **prompt** template. Triggered
instances are ordinary instances:

- **existing, live instance** — the event runs as its next turn (the session resumes);
- **missing instance** — a channel is created in the agents category and a new
  session starts (trigger's workdir/provider/model, else the defaults), exactly like `/new`;
- **channel deleted** — recreated, like `/resume`;
- **`/stop`ped instance** — skipped (default), or resumed if the trigger has `if_stopped: resume`.

So `/stop`, `/resume`, `/delete`, `/model` and `/list` all work on them. The
channel shows a `⚡ trigger … fired` note before the turn.

Manage triggers on the host (the token is printed once; only its SHA-256 is
stored in `data/triggers.json`); `/triggers` lists them in Discord:

```bash
.venv/bin/python -m harness.triggers add ci-myrepo \
    --instance "ci-myrepo" --workdir ~/code/myrepo --provider claude \
    --prompt "The {workflow} run on {branch} failed: {url}. Diagnose and fix, or ask me." \
    --dedupe "{workflow}:{sha}" --max-per-hour 6
.venv/bin/python -m harness.triggers list | rotate <name> | remove <name>
```

Safety: unknown trigger and bad token both get 401; events are capped at 64 KB;
each trigger is rate-limited and can de-duplicate on a key; event values are
inserted as single, length-capped lines and the full event is appended as a
fenced block that the agent is told is untrusted data, not instructions.

## Adding another provider

Implement the `AgentProvider` interface in `harness/providers/` (see `claude.py`
for a complete example) and import it at the bottom of `base.py`:

```python
from .base import AgentProvider, StreamEvent, register

@register("mycli")
class MyProvider(AgentProvider):
    models = {"fast": "my-fast-model"}  # aliases offered in /new and /model
    default_model = "fast"

    async def run_turn(self, workdir, prompt, session_id=None, model=None):
        cmd = ["mycli", "--model", self.resolve_model(model), "-p", prompt]
        # spawn, stream, yield StreamEvent(kind="text"/"done"/"error"),
        # and yield session_id on the "done" event
```

It then shows up as a `provider` choice in `/new`. The harness, instance store,
streaming UI, and resume-on-message logic are all provider-agnostic.

## Claude usage limits

When the account's Claude usage limit stops a turn (`harness/limits.py`), the
harness reads the reset time from the CLI's `rate_limit_event` (status
`rejected`, `resetsAt`) or from its error text ("resets 9pm"), and:

- posts "⏸️ Claude usage limit reached … continues at <time>" in the channel,
- records the cut-off turn in `data/usage_limit.json` (it survives a restart),
- makes every Claude turn wait until 1 minute after the reset (the limit is per
  account), without holding a turn slot, and offers no worklog tasks meanwhile,
- then gives each cut-off channel a turn to continue the work. A message you
  send meanwhile runs at that time too, together with the cut-off work.

If the reset time is unknown, it tries again after 30 minutes.

## Liveness: progress updates and the watchdog

While a turn runs long, the bot posts a short "⏳ still working" status in the
channel every `STATUS_UPDATE_INTERVAL` seconds (default 30 min) with elapsed
time, tool count, and how long since the agent last produced output.

`run.sh` also starts a separate **watchdog process** (`harness/watchdog.py`)
that can report failures the bot itself cannot. The bot rewrites
`data/heartbeat.json` every `HEARTBEAT_INTERVAL` seconds (default 30); the
watchdog checks it every `WATCHDOG_CHECK_INTERVAL` seconds (default 60) and
posts an alert via the Discord REST API when:

- the heartbeat is stale for more than `WATCHDOG_STALE_AFTER` seconds
  (default 180) → 🔴 "harness appears down", then 🟢 when it recovers;
- a turn produces no agent output for more than `WATCHDOG_STALL_AFTER` seconds
  (default 1800) while the harness is alive → ⚠️ "agent may be hung".

Alerts go to the channel with the active turn (or the last active channel).

## Repository layout

```
harness/
├── bot.py              # Discord client: slash commands, message turns, streaming replies
├── config.py           # .env loading and typed settings
├── instances.py        # channel ↔ instance mapping (data/instances.json)
├── discord_markdown.py # adapts replies to Discord's Markdown, splits long ones
├── logfile.py          # log files for each process, and the /logs export
├── guard.py            # keeps slash commands to this bot's own channels
├── pending.py          # queued messages: edits and deletes reach the agent
├── whatsapp.py         # optional WhatsApp mirror: one group per instance (neonize)
├── worklog.py          # shared worklog: event rules and the `wl` commands
├── gdoc.py            # `gdoc`: Markdown reports as Google Docs; gdoc_sync.py edits Docs in place
├── worklog_service.py  # its #worklog transport, local API for `wl`, check-ins, mail delivery
├── workdirs.py         # folder-by-folder workdir suggestions for /new
├── watchdog.py         # separate process: alerts when the harness dies or a turn stalls
├── uptime.py           # separate unit: internet/system up-down event log + /uptime stats
└── providers/
    ├── base.py         # AgentProvider ABC + registry
    ├── claude.py       # Claude Code CLI provider
    ├── kimi.py         # Kimi CLI provider
    └── muse.py         # Muse CLI provider (`muse exec --json`)
.env.example            # all configuration keys, documented
setup.sh                # Linux one-line installer and setup wizard
setup.ps1               # Windows one-line installer and setup wizard
bin/wl, bin/wl.cmd      # the `wl` command agents use (stdlib Python)
run.sh                  # starts the harness and the watchdog on Linux
run.ps1                 # starts the harness and the watchdog on Windows
```

## License

Copyright 2026 Johnny Lee

Licensed under the [Apache License, Version 2.0](LICENSE).

# discord_agent

Chat with coding-agent instances (Claude Code, Kimi Code and Muse, extensible to others) from
Discord — designed for driving agents from your phone.

- **One agent instance = one Discord channel.** Create instances with `/new`; each
  channel is a full chat session with an agent.
- **One Discord message = one agent turn.** The agent's reply streams into a single
  message in the channel (edited in place), and the channel topic always shows the
  agent's last message.
- **Sessions survive restarts.** The harness stores each instance's provider session
  ID on disk. If the harness (or the whole machine) goes down, the next message in
  the channel transparently resumes the agent exactly where it left off.

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
                                │  one subprocess per turn
                                ▼
                        claude CLI (`claude -p`) or kimi CLI (`kimi -p`)
                                │  sessions persisted by the CLI itself
                                ▼
                        ~/.claude/projects/  ·  ~/.kimi-code/sessions/
```

Every message in a managed channel runs one print-mode invocation — e.g.
`claude --model claude-opus-5-5 -p "<message>"`, plus `--resume <id>` once a session
exists (`kimi --session <id> -p ...` for Kimi). The CLI's own session store is what
makes instances durable; the harness just remembers the mapping between Discord
channels, providers, models and session IDs (`data/instances.json`).

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
- If the agent's CLI crashes before replying, the bot says so, restarts the session
  and resends your message; if it crashes again, your message goes to a fresh
  session (`/session` can switch back to the old one). A turn also ends as soon as
  the CLI exits, even if a program it started in the background still holds its
  output open (that used to leave a session silently "running" forever).
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
- `/new name:x provider:claude model:sonnet` picks the agent CLI and model.
  `provider` defaults to `DEFAULT_PROVIDER`; Claude's model defaults to
  `CLAUDE_MODEL` (`claude-opus-5-5`). Aliases `opus`, `sonnet`, `haiku`, `fable`
  are autocompleted; any full model ID works too.
- `/model` (inside an agent channel) shows the current provider and model.
  The `model` autocomplete lists every provider's models as `provider:alias`
  (e.g. `claude:sonnet`, `kimi:k3`, `muse:spark-1.3`); picking one from another
  provider switches provider too and starts a fresh session. Same-provider
  changes keep the session.
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
(`harness/discord_markdown.py`). Tables become an aligned code block when narrow
enough for a phone, otherwise a list with one entry per row. Headings below `###`
become bold lines, and `---` dividers are dropped. Replies over Discord's
2000-character limit are split at line breaks, and a code block cut by a split is
closed and reopened so both halves render. Code blocks are never changed.

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
├── guard.py            # keeps slash commands to this bot's own channels
├── pending.py          # queued messages: edits and deletes reach the agent
├── whatsapp.py         # optional WhatsApp mirror: one group per instance (neonize)
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
run.sh                  # starts the harness and the watchdog on Linux
run.ps1                 # starts the harness and the watchdog on Windows
```

## License

Copyright 2026 Johnny Lee

Licensed under the [Apache License, Version 2.0](LICENSE).

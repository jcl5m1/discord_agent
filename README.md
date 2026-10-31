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

That single command does the full setup, asking questions as it goes:

1. **Prerequisites:** installs `git`/`curl` if missing (via apt, needs sudo)
   and `uv`, which also provides Python 3.12.
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
8. **Services:** installs and starts the bot, watchdog and uptime systemd user
   services, with lingering on so they run at boot.

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

### 4. Use it (from any Discord client, including your phone)

- `/new name:my-project workdir:/home/you/code/my-project prompt:give me a summary of this repo`
  creates `#my-project` under the agents category and optionally starts it with a prompt.
- Just type in the channel — every message is a turn. Watch the 🤔 message update as
  the agent works. ✅ = turn finished, ❌ = error (check harness logs).
- `/new name:x provider:claude model:sonnet` picks the agent CLI and model.
  `provider` defaults to `DEFAULT_PROVIDER`; Claude's model defaults to
  `CLAUDE_MODEL` (`claude-opus-5-5`). Aliases `opus`, `sonnet`, `haiku`, `fable`
  are autocompleted; any full model ID works too.
- `/model` (inside an agent channel) shows the current provider and model.
  The `model` autocomplete lists every provider's models as `provider:alias`
  (e.g. `claude:sonnet`, `kimi:k3`, `muse:spark-1.3`); picking one from another
  provider switches provider too and starts a fresh session. Same-provider
  changes keep the session.
- `/list` shows active instances with their provider, model and session IDs;
  stopped instances are listed separately at the bottom.
- `/stop name:my-project` stops the instance (no replies, hidden from the active
  list) and locks the channel; the session is kept.
- `/resume name:my-project` brings a stopped instance back — unlocking its
  channel, or recreating the channel if it was deleted in Discord while the
  instance is still tracked. The session resumes on the next message.
- `/delete name:my-project` stops the instance (if active) and removes it for
  good — channel deleted, instance forgotten.
- `/new name:my-project` again reopens the channel and **resumes the same session**.
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
├── whatsapp.py         # optional WhatsApp mirror: one group per instance (neonize)
├── watchdog.py         # separate process: alerts when the harness dies or a turn stalls
├── uptime.py           # separate unit: internet/system up-down event log + /uptime stats
└── providers/
    ├── base.py         # AgentProvider ABC + registry
    ├── claude.py       # Claude Code CLI provider
    ├── kimi.py         # Kimi CLI provider
    └── muse.py         # Muse CLI provider (`muse exec --json`)
.env.example            # all configuration keys, documented
setup.sh                # one-line installer: prereqs, clone, venv, agent CLIs + logins, .env, services
run.sh                  # starts the harness and the watchdog
```

## License

Copyright 2026 Johnny Lee

Licensed under the [Apache License, Version 2.0](LICENSE).

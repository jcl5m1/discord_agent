# discord_agent

Chat with coding-agent instances (Claude Code and Kimi CLI, extensible to others) from
Discord — designed for driving agents from your phone.

- **One agent instance = one Discord channel.** Create instances with `/new`; each
  channel is a full chat session with an agent.
- **One Discord message = one agent turn.** The agent's reply streams into a single
  message in the channel (edited in place), and the channel topic always shows the
  agent's last message.
- **Sessions survive restarts.** The harness stores each instance's provider session
  ID on disk. If the harness (or the whole machine) goes down, the next message in
  the channel transparently resumes the agent exactly where it left off.

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
     **Change Nickname** (optional). Add **Manage Roles** too if you want `/stop` to
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
- `/model` (inside an agent channel) shows the current model; `/model model:haiku`
  switches it from the next message on, keeping the same session.
- `/list` shows all instances with their provider, model and session IDs.
- `/stop name:my-project` locks the channel; the session is kept.
- `/new name:my-project` again reopens the channel and **resumes the same session**.

### Rich output (tables, dashboards, styled HTML)

Discord can't render HTML in messages. If the agent's reply contains a fenced
` ```html ` block, the harness screenshots it with headless Chromium and posts
the PNG inline, alongside the raw `.html` as an attachment. To use it, ask for
what you want in an HTML code block, e.g.:

> make me a styled HTML table of the files in this repo with their sizes, in a ```html block

The image renders inline on mobile; tap the attachment for the source.

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

## Repository layout

```
harness/
├── bot.py              # Discord client: slash commands, message turns, streaming replies
├── config.py           # .env loading and typed settings
├── instances.py        # channel ↔ instance mapping (data/instances.json)
└── providers/
    ├── base.py         # AgentProvider ABC + registry
    ├── claude.py       # Claude Code CLI provider
    └── kimi.py         # Kimi CLI provider
.env.example            # all configuration keys, documented
run.sh                  # starts the harness
```

#!/usr/bin/env bash
# Install or update the Discord agent harness on this machine.
#
#   curl -fsSL https://raw.githubusercontent.com/jcl5m1/discord_agent/main/setup.sh | bash
#   # or, from a checkout:  ./setup.sh
#
# Clones (or updates) the repo, builds the venv, asks for the Discord IDs and
# token (plus optional WhatsApp / public-URL settings), writes .env, checks the
# token against Discord, and installs the systemd user services.
# Safe to rerun: current .env values are offered as defaults.
#
# Options:  --dir <path>   install location (default: this checkout, else ~/discord_agent)
#           --no-service   skip systemd install/start (use ./run.sh instead)
set -euo pipefail

REPO_URL="https://github.com/jcl5m1/discord_agent.git"
DIR="${DISCORD_AGENT_DIR:-}"
WITH_SERVICE=1
while [ $# -gt 0 ]; do
  case "$1" in
    --dir) DIR="$2"; shift 2 ;;
    --no-service) WITH_SERVICE=0; shift ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

bold() { printf '\n\033[1m%s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
warn() { printf '  \033[33m! %s\033[0m\n' "$*"; }
die()  { printf '\033[31merror: %s\033[0m\n' "$*" >&2; exit 1; }

# Prompts read the terminal directly so `curl ... | bash` stays interactive.
if [ -r /dev/tty ] && (exec < /dev/tty) 2>/dev/null; then TTY=/dev/tty; else TTY=/dev/stdin; fi

# ask VAR "Question" "default" [secret]
ask() {
  local __var=$1 q=$2 def=${3:-} secret=${4:-} ans shown
  if [ -n "$secret" ] && [ -n "$def" ]; then shown="keep current ••••${def: -4}"; else shown=$def; fi
  if [ -n "$secret" ]; then
    read -r -s -p "  $q${shown:+ [$shown]}: " ans < "$TTY"; echo
  else
    read -r -p "  $q${shown:+ [$shown]}: " ans < "$TTY"
  fi
  printf -v "$__var" '%s' "${ans:-$def}"
}

yesno() {  # yesno "Question" y|n
  local ans def=${2:-y}
  read -r -p "  $1 [$( [ "$def" = y ] && echo Y/n || echo y/N )]: " ans < "$TTY"
  ans=${ans:-$def}
  [[ "$ans" =~ ^[Yy] ]]
}

# --- 1. prerequisites ---
bold "1/7 Checking prerequisites"
for cmd in git curl; do
  command -v "$cmd" >/dev/null || die "$cmd is required (e.g. sudo apt install $cmd)"
done
if command -v uv >/dev/null; then
  info "uv found — the venv will use Python 3.12 (uv downloads it if needed)"
else
  command -v python3 >/dev/null || die "python3 is required (or install uv: https://docs.astral.sh/uv/)"
  python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' \
    || die "Python 3.10+ is required (found $(python3 --version))"
  python3 -c 'import venv, ensurepip' 2>/dev/null \
    || die "python3 venv support missing (e.g. sudo apt install python3-venv)"
  info "using $(python3 --version) (installing uv is recommended for a pinned 3.12)"
fi

# --- 2. code ---
bold "2/7 Getting the code"
if [ -z "$DIR" ]; then
  here=$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)
  if [ -n "$here" ] && [ -f "$here/harness/bot.py" ]; then DIR=$here; else DIR="$HOME/discord_agent"; fi
fi
DIR=${DIR/#\~/$HOME}
if [ -d "$DIR/.git" ]; then
  info "updating $DIR"
  git -C "$DIR" pull --ff-only || warn "git pull failed — continuing with the current checkout"
else
  info "cloning into $DIR"
  git clone "$REPO_URL" "$DIR"
fi
cd "$DIR"

# --- 3. venv + dependencies ---
bold "3/7 Installing Python dependencies"
if command -v uv >/dev/null; then
  [ -x .venv/bin/python ] || uv venv --python 3.12 .venv
  uv pip install --python .venv/bin/python -q -r requirements.txt
else
  [ -x .venv/bin/python ] || python3 -m venv .venv
  .venv/bin/python -m pip install -q --upgrade pip
  .venv/bin/python -m pip install -q -r requirements.txt
fi
info "installing headless Chromium for HTML rendering (repo-local .pw-browsers)"
if ! PLAYWRIGHT_BROWSERS_PATH=.pw-browsers .venv/bin/playwright install chromium >/dev/null; then
  warn "Chromium download failed — rerun setup later; only HTML rendering is affected"
fi
if ! PLAYWRIGHT_BROWSERS_PATH=.pw-browsers .venv/bin/python - <<'EOF' 2>/dev/null
import asyncio
from playwright.async_api import async_playwright
async def main():
    async with async_playwright() as p:
        await (await p.chromium.launch()).close()
asyncio.run(main())
EOF
then
  warn "Chromium can't start — it likely needs system libraries:"
  warn "  sudo $DIR/.venv/bin/playwright install-deps chromium"
fi

# --- 4. Discord bot (manual, one-time) ---
bold "4/7 Discord bot"
cur() { [ -f .env ] && sed -n "s/^$1=//p" .env | tail -1 | sed 's/^"\(.*\)"$/\1/' || true; }
if [ -z "$(cur DISCORD_BOT_TOKEN)" ]; then
  cat <<'EOF'
  Create the bot once at https://discord.com/developers/applications:
    • New Application → name it → Create
    • Bot → Reset Token → Copy (paste it below)
    • Bot → Privileged Gateway Intents → enable "Message Content Intent"
  This script prints the invite link once it has the token.
  For the IDs: Discord → User Settings → Advanced → Developer Mode on, then
    • right-click your server → Copy Server ID
    • create a category (e.g. "agents") → right-click → Copy Channel ID
EOF
fi

# --- 5. configuration ---
bold "5/7 Configuration (Enter keeps the value in brackets)"
ask TOKEN    "Discord bot token" "$(cur DISCORD_BOT_TOKEN)" secret
ask GUILD    "Server (guild) ID" "$(cur DISCORD_GUILD_ID)"
ask CATEGORY "Category ID for agent channels" "$(cur DISCORD_CATEGORY_ID)"
[ -n "$TOKEN" ] || die "a bot token is required"
[[ "$GUILD" =~ ^[0-9]{15,22}$ ]] || die "server ID should be a long number, got '$GUILD'"
[[ "$CATEGORY" =~ ^[0-9]{15,22}$ ]] || die "category ID should be a long number, got '$CATEGORY'"

ask PROVIDER "Default agent CLI (claude | kimi | muse)" "$(cur DEFAULT_PROVIDER | grep . || echo claude)"
[[ "$PROVIDER" =~ ^(claude|kimi|muse)$ ]] || die "provider must be claude, kimi or muse"
ask WORKDIR  "Default working directory for new agents" "$(cur DEFAULT_WORKDIR | grep . || echo "$HOME")"

info ""
info "Optional — press Enter to skip:"
ask PUBLIC_URL "Public base URL for live web pages (Cloudflare Tunnel)" "$(cur PUBLIC_BASE_URL)"
ask WA_OWNER   "Your WhatsApp number, with country code, to enable the mirror" "$(cur WHATSAPP_OWNER)"
WA_BOT=""
if [ -n "$WA_OWNER" ]; then
  ask WA_BOT "The bot's own WhatsApp number (separate account)" "$(cur WHATSAPP_BOT_NUMBER)"
fi

[ -f .env ] && cp .env .env.bak && info "previous .env saved as .env.bak"
# Render .env from .env.example: answered keys replaced, other existing values kept.
ANSWERS=$(printf '%s\n' \
  "DISCORD_BOT_TOKEN=$TOKEN" "DISCORD_GUILD_ID=$GUILD" "DISCORD_CATEGORY_ID=$CATEGORY" \
  "DEFAULT_PROVIDER=$PROVIDER" "DEFAULT_WORKDIR=$WORKDIR" "PUBLIC_BASE_URL=$PUBLIC_URL" \
  "WHATSAPP_OWNER=${WA_OWNER//[^0-9]/}" "WHATSAPP_BOT_NUMBER=${WA_BOT//[^0-9]/}") \
.venv/bin/python - <<'EOF'
import os, re
from pathlib import Path

def parse(text):
    out = {}
    for line in text.splitlines():
        m = re.match(r"^([A-Z_][A-Z0-9_]*)=(.*)$", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out

existing = parse(Path(".env").read_text()) if Path(".env").exists() else {}
answers = parse(os.environ["ANSWERS"])
lines, seen = [], set()
for line in Path(".env.example").read_text().splitlines():
    m = re.match(r"^([A-Z_][A-Z0-9_]*)=(.*)$", line)
    if m:
        key = m.group(1)
        seen.add(key)
        value = answers.get(key, existing.get(key, m.group(2)))
        line = f"{key}={value}"
    lines.append(line)
extra = {k: v for k, v in existing.items() if k not in seen}
if extra:
    lines += ["", "# --- kept from the previous .env ---"] + [f"{k}={v}" for k, v in extra.items()]
Path(".env").write_text("\n".join(lines) + "\n")
os.chmod(".env", 0o600)
EOF
info "wrote $DIR/.env (mode 600)"

# --- 6. checks ---
bold "6/7 Checking Discord and agent CLIs"
# The token goes in via a file descriptor so it never shows up in `ps`.
api() { curl -s -w '\n%{http_code}' -H @<(printf 'Authorization: Bot %s\n' "$TOKEN") "https://discord.com/api/v10$1"; }
jget() { .venv/bin/python -c "import json,sys; d=json.loads(sys.stdin.read() or '{}'); print(d.get('$1', ''))"; }
me=$(api /users/@me); code=${me##*$'\n'}; body=${me%$'\n'*}
if [ "$code" != 200 ]; then
  warn "Discord rejected the token (HTTP $code) — rerun setup with the right token"
else
  bot_id=$(jget id <<<"$body"); info "token OK — bot is $(jget username <<<"$body")"
  app=$(api /applications/@me); flags=$(jget flags <<<"${app%$'\n'*}")
  if [ -n "$flags" ] && [ $(( flags & (1<<18 | 1<<19) )) -eq 0 ]; then
    warn "Message Content Intent looks OFF — enable it under Bot → Privileged Gateway Intents"
  fi
  # Manage Channels, Add Reactions, View Channel, Send Messages, Manage Messages,
  # Embed Links, Attach Files, Read Message History, Manage Roles.
  perms=268561488
  invite="https://discord.com/oauth2/authorize?client_id=$bot_id&scope=bot+applications.commands&permissions=$perms"
  g=$(api "/guilds/$GUILD"); gcode=${g##*$'\n'}
  if [ "$gcode" = 200 ]; then
    info "bot is in server \"$(jget name <<<"${g%$'\n'*}")\""
  else
    warn "bot is not in that server yet (HTTP $gcode) — invite it, then rerun or just start it:"
    warn "  $invite"
  fi
  c=$(api "/channels/$CATEGORY"); ccode=${c##*$'\n'}
  if [ "$ccode" = 200 ]; then
    [ "$(jget type <<<"${c%$'\n'*}")" = 4 ] && info "category \"$(jget name <<<"${c%$'\n'*}")\" found" \
      || warn "ID $CATEGORY is a channel, not a category — copy the category's ID instead"
  elif [ "$gcode" = 200 ]; then
    warn "category $CATEGORY not visible to the bot (HTTP $ccode)"
  fi
fi
for cli in claude kimi muse; do
  case $cli in
    claude) path=$(command -v claude || true) ;;
    kimi)   path=$(ls ~/.kimi-code/bin/kimi 2>/dev/null || command -v kimi || true) ;;
    muse)   path=$(ls ~/.local/bin/muse 2>/dev/null || command -v muse || true) ;;
  esac
  if [ -n "$path" ]; then info "$cli: $path"
  elif [ "$cli" = "$PROVIDER" ]; then warn "$cli (your default) is not installed — install and log in before chatting"
  else info "$cli: not installed (optional)"; fi
done

# --- 7. services ---
bold "7/7 Services"
if [ "$WITH_SERVICE" = 0 ] || ! command -v systemctl >/dev/null || ! systemctl --user show-environment >/dev/null 2>&1; then
  info "skipping systemd — start the harness with: $DIR/run.sh"
  exit 0
fi
if yesno "Install and (re)start the systemd user services (bot, watchdog, uptime)?" y; then
  .venv/bin/python -c "from harness import service; service.sync(unit=service.BOT_UNIT)"
  systemctl --user enable --quiet discord-agent.service discord-agent-watchdog.service discord-agent-uptime.service
  systemctl --user restart discord-agent.service
  systemctl --user start discord-agent-uptime.service
  if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != yes ]; then
    info "enabling linger so the services run at boot without a login"
    loginctl enable-linger "$USER" 2>/dev/null || sudo loginctl enable-linger "$USER" \
      || warn "could not enable linger — services will only run while you're logged in"
  fi
  sleep 3
  if systemctl --user is-active --quiet discord-agent.service; then
    info "running ✓  logs: journalctl --user -u discord-agent -f"
  else
    warn "the bot didn't stay up — check: journalctl --user -u discord-agent -n 50"
  fi
else
  info "not installed — start it any time with: $DIR/run.sh"
fi

bold "Done"
info "In Discord: /new name:my-project prompt:hello"
[ -n "$WA_OWNER" ] && info "WhatsApp: run /whatsapp in Discord to link the bot account"
exit 0

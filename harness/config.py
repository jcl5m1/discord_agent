"""Load configuration from .env and expose typed settings."""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"


def _load_dotenv() -> None:
    """Minimal .env loader (key=value lines); real environment wins."""
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv()

# User-level bin dirs the agent CLIs install into (claude, muse: ~/.local/bin;
# kimi: ~/.kimi-code/bin). systemd's default PATH has none of them, so they are
# prepended here for this process and its agents, and baked into the unit files.
USER_BIN_DIRS: list[str] = [
    d for d in (
        os.path.expanduser(p)
        for p in (
            "~/.local/bin", "~/.kimi-code/bin", "~/.npm-global/bin", "~/.cargo/bin", "~/bin",
            "/usr/local/cuda/bin",  # nvcc etc. for agents doing GPU work
        )
    )
    if os.path.isdir(d)
]
os.environ["PATH"] = os.pathsep.join(
    [*USER_BIN_DIRS, *(p for p in os.environ.get("PATH", "").split(os.pathsep)
                       if p and p not in USER_BIN_DIRS)]
)

LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO").upper()

BOT_TOKEN: str = os.environ.get("DISCORD_BOT_TOKEN", "")
GUILD_ID: int = int(os.environ.get("DISCORD_GUILD_ID", "0"))
CATEGORY_ID: int = int(os.environ.get("DISCORD_CATEGORY_ID", "0"))

# Channels (names or IDs) where any bot sharing this server may take commands;
# elsewhere this bot only takes them in its own category and instance channels.
SHARED_CHANNELS: set[str] = {
    c.strip() for c in os.environ.get("DISCORD_SHARED_CHANNELS", "general").split(",") if c.strip()
}

DEFAULT_PROVIDER: str = os.environ.get("DEFAULT_PROVIDER", "kimi")
DEFAULT_WORKDIR: str = os.path.expanduser(os.environ.get("DEFAULT_WORKDIR", str(Path.home())))
MAX_CONCURRENT_TURNS: int = int(os.environ.get("MAX_CONCURRENT_TURNS", "4"))
# Messages sent while a turn runs go straight into it for agents that support
# that (Claude Code reads them at its next step); false = they wait their turn.
STEER_RUNNING_TURNS: bool = os.environ.get("STEER_RUNNING_TURNS", "true").lower() not in {"0", "false", "no"}
# Seconds a message sent into a running turn is held before the agent gets it,
# so a quick edit or delete in Discord changes or removes it first.
STEER_HOLD: float = float(os.environ.get("STEER_HOLD", "10"))
# Seconds a message to an idle agent waits before its turn starts, so deleting it
# (e.g. sent to the wrong channel) or editing it right away takes effect first.
SEND_DELAY: float = float(os.environ.get("SEND_DELAY", "5"))
# How long a graceful interrupt may take before the agent's CLI is killed.
INTERRUPT_GRACE: float = float(os.environ.get("INTERRUPT_GRACE", "15"))

# --- Liveness: in-channel progress updates + heartbeat for the watchdog ---
# While a turn runs long, post a short "still working" status this often.
STATUS_UPDATE_INTERVAL: int = int(os.environ.get("STATUS_UPDATE_INTERVAL", "1800"))
# The bot rewrites data/heartbeat.json this often; the watchdog process reads it.
HEARTBEAT_INTERVAL: int = int(os.environ.get("HEARTBEAT_INTERVAL", "30"))
HEARTBEAT_PATH: Path = DATA_DIR / "heartbeat.json"
# Watchdog: check cadence, when the harness counts as down, when a silent turn
# counts as stalled.
WATCHDOG_CHECK_INTERVAL: int = int(os.environ.get("WATCHDOG_CHECK_INTERVAL", "60"))
WATCHDOG_STALE_AFTER: int = int(os.environ.get("WATCHDOG_STALE_AFTER", "180"))
WATCHDOG_STALL_AFTER: int = int(os.environ.get("WATCHDOG_STALL_AFTER", "1800"))

KIMI_BIN: str = os.path.expanduser(os.environ.get("KIMI_BIN", "~/.kimi-code/bin/kimi"))
KIMI_MODEL: str | None = os.environ.get("KIMI_MODEL") or None
# Kimi's config.toml; its [models.*] tables populate /model autocomplete.
KIMI_CONFIG: str = os.path.expanduser(os.environ.get("KIMI_CONFIG", "~/.kimi-code/config.toml"))

MUSE_BIN: str = os.path.expanduser(os.environ.get("MUSE_BIN", "~/.local/bin/muse"))
# Default model for new muse instances (empty = muse's own settings.json default).
MUSE_MODEL: str | None = os.environ.get("MUSE_MODEL") or None
# Passed as --reasoning-effort (empty = muse's default for the model).
MUSE_EFFORT: str | None = os.environ.get("MUSE_EFFORT") or None

CLAUDE_BIN: str = os.path.expanduser(os.environ.get("CLAUDE_BIN", "claude"))
# Default model for new Claude instances (alias like "opus" or a full model ID).
CLAUDE_MODEL: str = os.environ.get("CLAUDE_MODEL") or "claude-opus-5-5"
# Passed as --permission-mode; bypassPermissions mirrors kimi's auto policy.
CLAUDE_PERMISSION_MODE: str = os.environ.get("CLAUDE_PERMISSION_MODE", "bypassPermissions")

# --- Agent sign-in: subscriptions, not API keys ---
# When set, these switch the agent CLIs from your subscription login to per-token
# API billing (Claude Code and muse prefer them over their own login). They are
# removed from this process, and so from every agent it starts, unless
# ALLOW_AGENT_API_KEYS=true. Sign in with /login in Discord instead.
AGENT_API_KEY_VARS: tuple[str, ...] = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "META_API_KEY", "MOONSHOT_API_KEY", "KIMI_API_KEY",
)
ALLOW_AGENT_API_KEYS: bool = os.environ.get("ALLOW_AGENT_API_KEYS", "").lower() in {"1", "true", "yes"}
# Names (never values) of the keys removed, so startup can say which were ignored.
IGNORED_API_KEYS: list[str] = []
if not ALLOW_AGENT_API_KEYS:
    for _var in AGENT_API_KEY_VARS:
        if os.environ.pop(_var, None) is not None:
            IGNORED_API_KEYS.append(_var)

# --- Shared worklog (all agents on the server, every machine; see worklog.py) ---
# How long a turn may stay open for the agent's background tasks (subagents, background commands) after its reply.
BACKGROUND_WAIT_MAX: float = float(os.environ.get("BACKGROUND_WAIT_MAX_HOURS", "12")) * 3600
# After a restart, give each agent whose turn (or background work) the restart cut off a resume turn.
AUTO_RESUME: bool = os.environ.get("AUTO_RESUME", "true").lower() not in {"0", "false", "no"}
WORKLOG_ENABLED: bool = os.environ.get("WORKLOG_ENABLED", "true").lower() not in {"0", "false", "no"}
# The channel every bot reads and writes the worklog through.
WORKLOG_CHANNEL: str = os.environ.get("WORKLOG_CHANNEL", "worklog")
# Local port for agents' `wl` command (outside the 8100-8999 agent server range).
WORKLOG_PORT: int = int(os.environ.get("WORKLOG_PORT", "8091"))
# Offer the best open, unblocked task to an agent that is idle with no claim (worklog_service._offer_work).
# Only agents with user activity in the last WORKLOG_OFFER_ACTIVE_DAYS days are offered work.
WORKLOG_OFFERS: bool = os.environ.get("WORKLOG_OFFERS", "1").lower() not in ("0", "false", "no", "off")
# Task areas that mean the same project, as folder=area pairs ("discord_agent=harness").
WORKLOG_AREA_ALIASES: dict[str, str] = dict(
    pair.split("=", 1) for pair in os.environ.get("WORKLOG_AREA_ALIASES", "discord_agent=harness").split(",")
    if "=" in pair)
WORKLOG_OFFER_ACTIVE_DAYS: float = float(os.environ.get("WORKLOG_OFFER_ACTIVE_DAYS", "2"))

# --- Uptime log (discord-agent-uptime.service, /uptime) ---
# Seconds between internet checks; only state changes are logged.
UPTIME_INTERVAL: float = float(os.environ.get("UPTIME_INTERVAL", "10"))
# Reports as Google Docs (harness/gdoc.py, the `gdoc` command): an rclone Google
# Drive remote, the Drive folder for the Docs, the registry of report -> Doc, and
# the people each Doc is shared with (one "email [role]" per line).
GDOC_REMOTE: str = os.environ.get("GDOC_REMOTE", "gdrive")
GDOC_FOLDER: str = os.environ.get("GDOC_FOLDER", "agent-reports")
GDOC_REGISTRY: Path = Path(os.path.expanduser(os.environ.get("GDOC_REGISTRY", "") or str(DATA_DIR / "gdocs.json")))
GDOC_SHARE_FILE: Path = Path(os.path.expanduser(
    os.environ.get("GDOC_SHARE_FILE", "") or "~/.config/discord-agent/gdoc_share"))
GDOC_WEB_REGISTRY: Path | None = (Path(os.path.expanduser(os.environ["GDOC_WEB_REGISTRY"]))
                                  if os.environ.get("GDOC_WEB_REGISTRY") else None)
# Memory recorder in the uptime monitor (harness/memmon.py): top processes by memory, each minute.
MEM_MONITOR: bool = os.environ.get("MEM_MONITOR", "1").lower() not in ("0", "false", "no", "off")
# System health (harness/health.py): memory levels, the agent told to prevent a crash, the last-resort action.
HEALTH_WARN_PCT: float = float(os.environ.get("HEALTH_WARN_PCT", "20"))  # available RAM % for `warn`
HEALTH_CRIT_PCT: float = float(os.environ.get("HEALTH_CRIT_PCT", "6"))  # available RAM % for `critical`
HEALTH_ACTIONS: bool = os.environ.get("HEALTH_ACTIONS", "1").lower() not in ("0", "false", "no", "off")
HEALTH_AGENT: str = os.environ.get("HEALTH_AGENT", "")  # instance name; default: the one in the harness repo
# GPU flight recorder in the uptime monitor (harness/gpumon.py); on if nvidia-smi exists.
GPU_MONITOR: bool = os.environ.get("GPU_MONITOR", "1").lower() not in ("0", "false", "no", "off")
# host:port pairs probed with a TCP connect; the internet is up if any answers.
UPTIME_TARGETS: list[tuple[str, int]] = [
    (hp.rsplit(":", 1)[0], int(hp.rsplit(":", 1)[1]))
    for hp in os.environ.get("UPTIME_TARGETS", "1.1.1.1:53,8.8.8.8:53,9.9.9.9:53").split(",")
    if ":" in hp
]

# --- WhatsApp mirror (optional) ---
# Owner's WhatsApp number with country code (e.g. 15551234567). Setting it
# enables the bridge: the harness links as a device of a separate bot account
# and creates one "<name>-bot" group per instance with this number in it.
WHATSAPP_OWNER: str = os.environ.get("WHATSAPP_OWNER", "").strip()
WHATSAPP_ENABLED: bool = bool(WHATSAPP_OWNER)
# The bot account's own number; lets /whatsapp hand out a pairing code by default.
WHATSAPP_BOT_NUMBER: str = os.environ.get("WHATSAPP_BOT_NUMBER", "").strip()

# --- Live web output through an existing Cloudflare Tunnel ---
# Public base URL under which /agents/<slug>/<token>/<port>/ is reachable.
# Empty = proxy/URL features disabled.
PUBLIC_BASE_URL: str = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
PROXY_PORT: int = int(os.environ.get("AGENT_PROXY_PORT", "8471"))
# Reserved port range for agent-served web pages; the harness only announces
# listening ports inside this range.
AGENT_PORT_START: int = int(os.environ.get("AGENT_PORT_START", "8100"))
AGENT_PORT_END: int = int(os.environ.get("AGENT_PORT_END", "8999"))

if PUBLIC_BASE_URL:
    # Agent subprocesses can read this to know where their servers are exposed.
    os.environ["AGENT_PUBLIC_BASE"] = PUBLIC_BASE_URL

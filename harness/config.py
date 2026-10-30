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

DEFAULT_PROVIDER: str = os.environ.get("DEFAULT_PROVIDER", "kimi")
DEFAULT_WORKDIR: str = os.path.expanduser(os.environ.get("DEFAULT_WORKDIR", str(Path.home())))
MAX_CONCURRENT_TURNS: int = int(os.environ.get("MAX_CONCURRENT_TURNS", "4"))

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

# --- Uptime log (discord-agent-uptime.service, /uptime) ---
# Seconds between internet checks; only state changes are logged.
UPTIME_INTERVAL: float = float(os.environ.get("UPTIME_INTERVAL", "10"))
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

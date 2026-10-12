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

BOT_TOKEN: str = os.environ.get("DISCORD_BOT_TOKEN", "")
GUILD_ID: int = int(os.environ.get("DISCORD_GUILD_ID", "0"))
CATEGORY_ID: int = int(os.environ.get("DISCORD_CATEGORY_ID", "0"))

DEFAULT_PROVIDER: str = os.environ.get("DEFAULT_PROVIDER", "kimi")
DEFAULT_WORKDIR: str = os.environ.get("DEFAULT_WORKDIR", str(Path.home()))
MAX_CONCURRENT_TURNS: int = int(os.environ.get("MAX_CONCURRENT_TURNS", "4"))

KIMI_BIN: str = os.path.expanduser(os.environ.get("KIMI_BIN", "~/.kimi-code/bin/kimi"))
KIMI_MODEL: str | None = os.environ.get("KIMI_MODEL") or None

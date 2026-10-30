"""Persistent mapping between Discord channels and agent instances.

Stored in data/instances.json (gitignored):

{
  "<channel_id>": {
    "name": "my-project",
    "provider": "kimi",
    "workdir": "/home/user/code/my-project",
    "session_id": "session_xxxx",
    "model": "opus",
    "wa_group": "120363000000000000"
  }
}

session_id is captured from the provider after its first turn; from then on the
harness resumes it on every new message, so instances survive harness restarts.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from . import config

log = logging.getLogger(__name__)


def _claude_project_dir(cwd: str) -> Path:
    """Where Claude Code keeps session transcripts for a given cwd."""
    return Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", cwd)


def _migrate_claude_session(old_cwd: str, new_cwd: str, session_id: str) -> None:
    """Move a transcript so `claude --resume` still finds it under the new cwd."""
    src = _claude_project_dir(old_cwd) / f"{session_id}.jsonl"
    dst = _claude_project_dir(new_cwd) / f"{session_id}.jsonl"
    if src.exists() and not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        log.info("migrated claude session %s: %s -> %s", session_id, src.parent, dst.parent)


@dataclass
class Instance:
    channel_id: int
    name: str
    provider: str
    workdir: str
    session_id: str | None = None
    model: str | None = None  # alias or model ID; None = provider default
    stopped: bool = False  # set by /stop, cleared when /new reopens the instance
    last_message_id: str | None = None  # newest user message processed (catch-up marker)
    web_token: str | None = None  # guards the public /agents/<slug>/<token>/ proxy path
    wa_group: str | None = None  # WhatsApp group JID user part (mirror chat), if any

    def __post_init__(self) -> None:
        # Subprocesses get workdir as cwd verbatim, so "~" must be expanded here
        # or it becomes a literal directory under the harness's own cwd.
        self.workdir = os.path.abspath(os.path.expanduser(self.workdir))

    def ensure_web_token(self) -> str:
        if not self.web_token:
            import secrets

            self.web_token = secrets.token_hex(8)
        return self.web_token


class InstanceManager:
    def __init__(self, path: Path | None = None):
        self.path = path or (config.DATA_DIR / "instances.json")
        self._instances: dict[int, Instance] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        raw = json.loads(self.path.read_text())
        migrated = False
        for cid, data in raw.items():
            inst = Instance(channel_id=int(cid), **data)
            self._instances[int(cid)] = inst
            if inst.workdir != data["workdir"]:
                # Saved before workdirs were normalized; the old value resolved
                # relative to the harness cwd, so that's where its sessions live.
                migrated = True
                if inst.provider == "claude" and inst.session_id:
                    _migrate_claude_session(
                        os.path.abspath(data["workdir"]), inst.workdir, inst.session_id
                    )
        if migrated:
            self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        raw = {str(cid): asdict(inst) for cid, inst in self._instances.items()}
        raw = {cid: {k: v for k, v in d.items() if k != "channel_id"} for cid, d in raw.items()}
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(raw, f, indent=2)
        os.replace(tmp, self.path)

    def get(self, channel_id: int) -> Instance | None:
        return self._instances.get(channel_id)

    def by_name(self, name: str) -> Instance | None:
        for inst in self._instances.values():
            if inst.name == name:
                return inst
        return None

    def put(self, inst: Instance) -> None:
        # Drop stale entries for the same instance (e.g. after a reopen moved
        # it to a new channel) so by_name() never sees duplicates.
        for cid in [c for c, i in self._instances.items() if i.name == inst.name and c != inst.channel_id]:
            del self._instances[cid]
        self._instances[inst.channel_id] = inst
        self._save()

    def remove(self, channel_id: int) -> None:
        self._instances.pop(channel_id, None)
        self._save()

    def all(self) -> list[Instance]:
        return list(self._instances.values())

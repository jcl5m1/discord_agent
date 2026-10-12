"""Persistent mapping between Discord channels and agent instances.

Stored in data/instances.json (gitignored):

{
  "<channel_id>": {
    "name": "my-project",
    "provider": "kimi",
    "workdir": "/home/user/code/my-project",
    "session_id": "session_xxxx"
  }
}

session_id is captured from the provider after its first turn; from then on the
harness resumes it on every new message, so instances survive harness restarts.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from . import config


@dataclass
class Instance:
    channel_id: int
    name: str
    provider: str
    workdir: str
    session_id: str | None = None


class InstanceManager:
    def __init__(self, path: Path | None = None):
        self.path = path or (config.DATA_DIR / "instances.json")
        self._instances: dict[int, Instance] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        raw = json.loads(self.path.read_text())
        for cid, data in raw.items():
            self._instances[int(cid)] = Instance(channel_id=int(cid), **data)

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
        self._instances[inst.channel_id] = inst
        self._save()

    def remove(self, channel_id: int) -> None:
        self._instances.pop(channel_id, None)
        self._save()

    def all(self) -> list[Instance]:
        return list(self._instances.values())

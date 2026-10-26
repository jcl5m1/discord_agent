"""Provider abstraction: a coding-agent CLI the harness can drive turn-by-turn.

A provider runs one prompt as a subprocess (non-interactive CLI mode) and yields
text events as they arrive. Each agent instance keeps whatever session identifier
the provider returns, so instances survive harness restarts as long as the CLI's
own session store does.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncIterator


@dataclass
class TurnResult:
    """Result of one provider turn."""

    text: str = ""  # full assistant text for the turn
    session_id: str | None = None  # provider session id (capture on first turn)
    error: str | None = None


@dataclass
class StreamEvent:
    """Incremental event while a turn is running.

    kind is one of: "text", "status", "done", "error".
    "done" events carry the final text and, when known, the provider session id.
    """

    kind: str
    text: str = ""
    session_id: str | None = None


class AgentProvider(ABC):
    """Interface every agent CLI integration must implement."""

    name: str = "abstract"
    # Short model aliases -> full model IDs (offered as choices in Discord).
    models: dict[str, str] = {}
    # Model used when an instance has none set (None = the CLI's own default).
    default_model: str | None = None

    def resolve_model(self, model: str | None) -> str | None:
        """Map an alias (or None) to the model ID passed to the CLI."""
        model = model or self.default_model
        return self.models.get(model, model) if model else None

    @abstractmethod
    def run_turn(
        self, workdir: str, prompt: str, session_id: str | None = None, model: str | None = None
    ) -> AsyncIterator[StreamEvent]:
        """Run one prompt against the agent CLI.

        If session_id is given, resume that session; otherwise start a new one.
        model is the instance's model (alias or ID); None means default_model.
        Yields StreamEvents as output arrives, ending with a "done" or "error"
        event whose text carries the final text / error message.
        """


_REGISTRY: dict[str, type[AgentProvider]] = {}


def register(name: str):
    def wrap(cls: type[AgentProvider]) -> type[AgentProvider]:
        cls.name = name
        _REGISTRY[name] = cls
        return cls

    return wrap


def get_provider(name: str) -> AgentProvider:
    if name not in _REGISTRY:
        raise KeyError(
            f"unknown provider '{name}', available: {sorted(_REGISTRY)}"
        )
    return _REGISTRY[name]()


def provider_names() -> list[str]:
    return sorted(_REGISTRY)


# Import built-in providers so they self-register.
from . import claude, kimi  # noqa: E402,F401

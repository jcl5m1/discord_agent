"""Track Discord messages waiting for (or in) an agent turn, so edits and
deletes reach the agent.

A message waits for its channel's turn lock before the agent sees it. While it
waits, an edit replaces its text and a delete drops it. Once the agent has it
(the turn started or finished), an edit queues a follow-up turn that shows the
agent both versions; a further edit while that follow-up waits updates it.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass

# Finished messages whose last text is remembered, to recognise later edits.
REMEMBER = 500


@dataclass
class PendingTurn:
    message_id: int
    content: str
    edit_of: str | None = None  # earlier text, when this turn is about an edit
    channel_id: int | None = None
    started: bool = False
    deleted: bool = False

    def prompt(self) -> str:
        if self.edit_of is None:
            return self.content
        return (
            "[The user edited a message you already received. Earlier version:]\n"
            f"{self.edit_of}\n\n[Edited version:]\n{self.content}"
        )


class PendingTurns:
    def __init__(self, remember: int = REMEMBER):
        self._turns: dict[int, list[PendingTurn]] = {}
        self._done: OrderedDict[int, str] = OrderedDict()
        self._remember = remember

    def add(
        self, message_id: int, content: str, edit_of: str | None = None, channel_id: int | None = None
    ) -> PendingTurn:
        turn = PendingTurn(message_id, content, edit_of, channel_id=channel_id)
        self._turns.setdefault(message_id, []).append(turn)
        return turn

    def finish(self, turn: PendingTurn) -> None:
        turns = self._turns.get(turn.message_id, [])
        if turn in turns:
            turns.remove(turn)
        if not turns:
            self._turns.pop(turn.message_id, None)
        if turn.started:
            self._done[turn.message_id] = turn.content
            self._done.move_to_end(turn.message_id)
            while len(self._done) > self._remember:
                self._done.popitem(last=False)

    def edit(
        self, message_id: int, content: str, channel_id: int | None = None
    ) -> tuple[str, PendingTurn | None]:
        """Apply an edit: ("updated", turn), ("followup", new turn) or ("ignored", None).

        Edits that don't change the text (Discord sends one when it adds a link
        preview) and edits of messages this process never handled are ignored.
        """
        turns = self._turns.get(message_id, [])
        waiting = next((t for t in reversed(turns) if not t.started and not t.deleted), None)
        if waiting is not None:
            if waiting.content == content:
                return "ignored", None
            waiting.content = content
            return "updated", waiting
        latest = turns[-1].content if turns else self._done.get(message_id)
        if latest is None or latest == content:
            return "ignored", None
        return "followup", self.add(message_id, content, edit_of=latest, channel_id=channel_id)

    def remember(self, message_id: int, content: str) -> None:
        """Record a message the agent got without queueing (sent into a running
        turn), so a later edit still becomes a follow-up."""
        self._done[message_id] = content
        self._done.move_to_end(message_id)
        while len(self._done) > self._remember:
            self._done.popitem(last=False)

    def drop_channel(self, channel_id: int) -> int:
        """Drop every turn waiting in a channel (after /interrupt); return how many."""
        dropped = 0
        for turns in self._turns.values():
            for turn in turns:
                if turn.channel_id == channel_id and not turn.started and not turn.deleted:
                    turn.deleted = True
                    dropped += 1
        return dropped

    def delete(self, message_id: int) -> int:
        """Drop the message's turns that haven't started; return how many."""
        dropped = 0
        for turn in self._turns.get(message_id, []):
            if not turn.started and not turn.deleted:
                turn.deleted = True
                dropped += 1
        self._done.pop(message_id, None)
        return dropped

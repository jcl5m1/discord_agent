"""Keep this bot's slash commands to its own channels.

Several harness bots can share one Discord server, each with its own category.
Discord offers every bot's commands in every channel, and doesn't let a bot hide
them from a server owner or admin, so a command invoked in another bot's channel
is refused instead.
"""

from __future__ import annotations


def channel_allowed(
    channel, *, category_id: int, shared: set[str], instance_channel_ids: set[int]
) -> bool:
    """Whether commands may run in this channel on this bot.

    shared holds channel names or IDs. Threads count as their parent channel.
    """
    channel = getattr(channel, "parent", None) or channel
    return (
        channel.id in instance_channel_ids
        or getattr(channel, "category_id", None) == category_id
        or getattr(channel, "name", None) in shared
        or str(channel.id) in shared
    )

"""The Matrix adapter's ``MessageEvent.reply_expected`` verdict.

Extracted from ``adapter.py`` to keep that module inside its FILE_LINES cap; semantics only
grow here (the multi-bot-room silence-fallback storm, PR #46).
"""
from __future__ import annotations

from gateway.platforms.event import MessageType

__all__ = ["verdict"]


def verdict(
    *,
    is_dm: bool,
    is_mentioned: bool,
    is_command: bool,
    reply_to_is_own: bool,
    owns_replied_event: bool,
) -> bool | None:
    """Whether the gateway may replace this event's silence marker with the visible fallback.

    ``True`` — addressed to the bot (mention, slash command, a reply to its own message): the
    fallback must stay armed, a human ping must never vanish. ``False`` — admitted into a group
    room but unaddressed (free-room chatter, bot-thread follow-ups, peer-bot warnings): a bare
    silence marker may stand, which is what stops peer bots from re-ingesting our fallback and
    re-triggering the same verdict (the multi-bot-room warning storm). ``None`` — DM: addressed
    by construction, the fallback must stay (see the field docs on MessageEvent).
    """
    if is_dm:
        return None
    if is_mentioned or is_command or reply_to_is_own or owns_replied_event:
        return True
    return False

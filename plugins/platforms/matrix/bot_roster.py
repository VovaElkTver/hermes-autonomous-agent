"""L3 (TZ-memdoor-5090-v2): roster mention expansion for bot-to-bot sends.

The adapter already ships m.mentions for FULL ``@user:server`` tokens
(``_extract_outbound_mentions``), but agents naturally write ``@hermes`` — a
bare localpart the receiver never resolves, so the peer bot sleeps through the
message (dead address, silent). When enabled, bare roster aliases expand to
their full MXIDs outside code spans, and the existing mention machinery does
the rest. Aliases come from config — never guessed from display names.

OFF by default (``bot_relay_mentions.enabled`` / ``HERMES_BOT_RELAY_MENTIONS``)
— text passes through byte-identical.
"""

from __future__ import annotations

import os
import re

_CODE_SPAN = re.compile(r"(```.*?```|`[^`\n]*`)", re.DOTALL)


def bot_relay_mentions_enabled() -> bool:
    env = os.environ.get("HERMES_BOT_RELAY_MENTIONS")
    if env is not None:
        return env.strip().lower() in ("1", "true", "yes", "on")
    try:
        from hermes_cli.config import load_config
        cfg = load_config() or {}
        return bool((cfg.get("bot_relay_mentions") or {}).get("enabled"))
    except Exception:
        return False


def roster() -> dict:
    try:
        from hermes_cli.config import load_config
        cfg = load_config() or {}
        raw = (cfg.get("bot_relay_mentions") or {}).get("roster") or {}
        return {str(k).lstrip("@"): str(v) for k, v in raw.items()}
    except Exception:
        return {}


def _expand_plain(text: str, aliases: dict) -> str:
    for alias, mxid in aliases.items():
        if not mxid.startswith("@") or ":" not in mxid:
            continue
        # bare alias only: not @alias:server (already full), not inside a word
        text = re.sub(r"(?<![\w@])@" + re.escape(alias) + r"(?![:\w])", mxid, text)
    return text


def expand_outside_code(text: str, aliases: dict | None = None) -> str:
    """Expand bare roster aliases to MXIDs, leaving code spans untouched."""
    if not text:
        return text
    aliases = roster() if aliases is None else aliases
    if not aliases:
        return text
    parts = _CODE_SPAN.split(text)
    for i in range(0, len(parts), 2):  # even indices = outside code spans
        parts[i] = _expand_plain(parts[i], aliases)
    return "".join(parts)


def maybe_expand(text: str) -> str:
    if not bot_relay_mentions_enabled():
        return text
    return expand_outside_code(text)

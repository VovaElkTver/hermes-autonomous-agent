"""Matrix ``!command`` → Hermes ``/command`` aliasing.

Extracted from ``adapter.py`` to keep that module inside its FILE_LINES cap (the ratchet's
"move an existing function into a topical sibling" remedy). Matrix clients reserve ``/`` for
their own UI, so this platform accepts ``!command`` as the dispatch sigil — but only for
tokens that resolve to known gateway/skill commands, so ordinary exclamations stay chat text.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

__all__ = ["MATRIX_BANG_COMMAND_RE", "normalize_matrix_bang_command", "resolve_matrix_bang_command"]

MATRIX_BANG_COMMAND_RE = re.compile(r"^!([A-Za-z][A-Za-z0-9_-]*)(?=$|\s)(.*)$", re.DOTALL)


def resolve_matrix_bang_command(name: str) -> str | None:
    """Resolve a ``!command`` token (Matrix clients reserve ``/``) to a dispatchable token.
    Only known gateway/skill commands resolve, so ordinary exclamations stay chat text. Returns
    whichever candidate resolved — raw lowercased first, then ``_``→``-`` — never a forced
    canonical form: aliases pass through for the dispatcher."""
    if not name:
        return None
    candidates = list(dict.fromkeys((name.lower(), name.lower().replace("_", "-"))))
    try:
        from hermes_cli.commands import is_gateway_known_command
        for candidate in candidates:
            if is_gateway_known_command(candidate):
                return candidate
    except Exception:
        logger.debug("Matrix: is_gateway_known_command failed for %r", name, exc_info=True)
    try:
        from agent.skill_commands import get_skill_commands
        skill_commands = get_skill_commands() or {}  # keys are slash-prefixed ("/arxiv")
        for candidate in candidates:
            if f"/{candidate}" in skill_commands:
                return candidate
    except Exception:
        logger.debug("Matrix: get_skill_commands failed for %r", name, exc_info=True)
    return None


def normalize_matrix_bang_command(text: str) -> str:
    """Convert Matrix ``!command`` aliases to normal Hermes ``/command`` text."""
    if not text or not text.startswith("!"):
        return text
    match = MATRIX_BANG_COMMAND_RE.match(text)
    if match is None:
        return text
    resolved = resolve_matrix_bang_command(match.group(1))
    if resolved is None:
        return text
    return f"/{resolved}{match.group(2) or ''}"

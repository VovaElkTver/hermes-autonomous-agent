"""fork_profile — one-key agent-first defaults for the VovaElkTver fork.

Upstream ships consumer-friendly defaults; this fork runs as an autonomous agent
first. ``autonomous_mode: true`` (top-level key in config.yaml) activates the
deviation bundle in :data:`BUNDLE`. Resolution order per knob:

1. an explicit setting (YAML bridge value / platform env variable) always wins;
2. ``autonomous_mode`` on → the bundle value;
3. otherwise the donor default — behavior byte-for-byte upstream, which keeps
   sync merges conflict-free: our deviations live here plus one call site per
   knob instead of smeared literal edits.

A second flag, ``administrative_mode``, is reserved for a sub-mode (the special
«personal manager» agent: strict rulebook, dedicated toolset); it nests INSIDE
autonomous_mode and is defined by its own spec later. This module only exposes
:meth:`administrative_mode` so callers can gate on it before the agent exists.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: (section, key) -> bundle value taken when autonomous_mode is on and nothing
#: explicit was set for that knob. Every entry must have a call site of
#: :func:`effective`; dead entries are deleted, not accumulated.
BUNDLE: dict[tuple[str, str], Any] = {
    # Matrix threads are a client-side visualizer, never session routing
    # (fork doctrine 04.11): branching = /branch + session_branches.
    ("matrix", "auto_thread"): False,
    ("matrix", "dm_mention_threads"): False,
    ("matrix", "thread_sessions"): False,
    ("matrix", "thread_delivery"): False,
}


def _hermes_home():
    from hermes_constants import get_hermes_home

    return get_hermes_home()


def _flag(name: str) -> bool:
    """Live read of a top-level bool key from config.yaml; errors fail OPEN."""
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly()
        node: Any = (cfg or {}).get(name) if isinstance(cfg, dict) else None
        if node is None:
            return False
        if isinstance(node, bool):
            return node
        return str(node).strip().lower() in {"true", "1", "yes"}
    except Exception as e:  # a broken config read must not break callers
        logger.debug("fork_profile: config read failed, %s treated as off: %s", name, e)
        return False


def autonomous_mode() -> bool:
    return _flag("autonomous_mode")


def administrative_mode() -> bool:
    """Sub-mode of :func:`autonomous_mode` — the «personal manager» agent switch.
    Never on unless the parent mode is on."""
    return autonomous_mode() and _flag("administrative_mode")


def effective(
    section: str,
    key: str,
    *,
    explicit: Optional[Any],
    upstream_default: Any,
) -> Any:
    """Resolve one knob.

    *explicit* is what the caller parsed from its own config/env layer — pass
    ``None`` when nothing was set anywhere (do NOT pass the caller's sentinel
    default: ``None`` means "unset"). Returns the bundled value when
    autonomous_mode is on and nothing explicit outranks it.
    """
    if explicit is not None:
        return _truthy(explicit)
    if (section, key) in BUNDLE and autonomous_mode():
        return BUNDLE[(section, key)]
    return upstream_default


def _truthy(value: Any) -> bool:
    """Same true/1/yes semantics the gateway adapters use for platform knobs."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes"}

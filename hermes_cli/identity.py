"""Instance identity: identity.name / identity.id in config.yaml.

A HERMES_HOME *instance* is the concrete running agent behind one home
directory.  Its identity is two config keys:

- ``identity.name`` — external label of the instance (a slug).  Default
  ``"hermes"`` means "unset" so legacy consumers keep their hardcoded
  strings.  Must match ``^[a-z0-9][a-z0-9._-]{0,30}[a-z0-9]$`` (1-32 chars).
- ``identity.id``   — optional stable instance identifier used by the
  registry (``identity_resolver``) for transport-name → position binding.

Paths are NOT derived from these values.  ``HERMES_HOME=~/.hermes-<label>``
remains a *convention*, not an identity (Architect decision, 20 Oct 2025).
"""

from __future__ import annotations

import re

__all__ = [
    "DEFAULT_INSTANCE_NAME",
    "identity_id",
    "identity_name",
    "instance_label",
    "validate_instance_slug",
]

DEFAULT_INSTANCE_NAME: str = "hermes"

_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,30}[a-z0-9])?$")


def validate_instance_slug(value: str) -> bool:
    """Return ``True`` when *value* is a valid instance-name slug."""
    return bool(_SLUG_RE.match(str(value or "")))


def _identity_block() -> dict:
    """Return the ``identity`` dict from the active profile config.

    Returns an empty dict on any failure so callers fall back to defaults.
    """
    try:
        from hermes_cli.config import load_config_readonly  # noqa: WPS433

        cfg = load_config_readonly() or {}
    except Exception:
        return {}
    block = cfg.get("identity")
    return block if isinstance(block, dict) else {}


def identity_name() -> str:
    """Return the configured ``identity.name``, defaulting to ``"hermes"``."""
    name = str(_identity_block().get("name") or "").strip()
    return name or DEFAULT_INSTANCE_NAME


def instance_label() -> str | None:
    """Return the configured name, or ``None`` when unset / default.

    Consumers that must NOT change behaviour when identity is left at the
    default use this: ``instance_label() or "<legacy default>"``.
    """
    name = str(_identity_block().get("name") or "").strip()
    if not name or name == DEFAULT_INSTANCE_NAME:
        return None
    return name


def identity_id() -> str | None:
    """Return ``identity.id`` or ``None``."""
    ident = str(_identity_block().get("id") or "").strip()
    return ident or None

"""Transport-name → position_id resolver for agent-to-agent envelopes.

Canonical identity for inter-agent exchange is the **position_id**.
Transport names (Matrix localpart, A2A agent name, MQ subject, origin_json
sender, etc.) are *bindings* in a registry and are resolved through this
module — they never silently substitute for the canonical id.

An unregistered transport resolves to the explicit literal
``"unregistered"``, never to a quiet ``"hermes"``.

See ``docs/envelope-spec.md`` for the full wire format.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

__all__ = [
    "UNREGISTERED",
    "VALID_KINDS",
    "Binding",
    "IdentityResolver",
    "format_envelope",
    "append_event",
]

#: Returned when a transport name has no registry binding.
UNREGISTERED: str = "unregistered"

#: Allowed values for the ``kind`` field in the envelope.
VALID_KINDS: tuple[str, ...] = (
    "информирование",
    "консультация",
    "просьба",
    "принятие",
    "согласование",
    "проверка",
    "возражение",
    "передача",
    "эскалация",
    "улучшение",
)


@dataclass(frozen=True)
class Binding:
    """A single transport → position mapping."""

    transport: str
    position_id: str
    host: str = ""
    path: str = ""
    notes: str = ""


class IdentityResolver:
    """Resolve transport names to canonical position ids.

    Usage::

        resolver = IdentityResolver.from_registry("registry.json")
        pid = resolver.resolve("matrix:@alice:mx.elk.mobi")
        # → "agent-alice-001"  or  "unregistered"
    """

    def __init__(self, bindings: Optional[dict[str, Binding]] = None) -> None:
        self._bindings: dict[str, Binding] = dict(bindings or {})

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def from_registry(cls, path: str | Path) -> "IdentityResolver":
        """Load bindings from a JSON registry file.

        Expected schema::

            {
              "bindings": [
                {"transport": "matrix:@alice:mx.elk.mobi",
                 "position_id": "agent-alice-001",
                 "host": "10.0.0.2", "path": "/srv/agents/alice"},
                ...
              ]
            }

        Missing file or malformed JSON → empty resolver (all unregistered).
        """
        try:
            raw = Path(path).read_text(encoding="utf-8-sig")
            data: dict[str, Any] = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            return cls()

        out: dict[str, Binding] = {}
        for row in data.get("bindings", []):
            transport = str(row.get("transport") or "").strip()
            position_id = str(row.get("position_id") or "").strip()
            if not transport or not position_id:
                continue
            out[transport] = Binding(
                transport=transport,
                position_id=position_id,
                host=str(row.get("host") or ""),
                path=str(row.get("path") or ""),
                notes=str(row.get("notes") or ""),
            )
        return cls(out)

    @classmethod
    def from_dict(cls, bindings: dict[str, Binding]) -> "IdentityResolver":
        """Create from an in-memory mapping (useful in tests)."""
        return cls(bindings)

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def register(
        self,
        transport: str,
        position_id: str,
        *,
        host: str = "",
        path: str = "",
        notes: str = "",
    ) -> None:
        """Add or update a binding."""
        if not transport.strip() or not position_id.strip():
            raise ValueError("transport and position_id must be non-empty")
        self._bindings[transport.strip()] = Binding(
            transport=transport.strip(),
            position_id=position_id.strip(),
            host=host,
            path=path,
            notes=notes,
        )

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def resolve(self, transport: str) -> str:
        """Return the position_id for *transport*, or ``UNREGISTERED``."""
        binding = self._bindings.get(str(transport or "").strip())
        if binding is None:
            return UNREGISTERED
        return binding.position_id

    def is_registered(self, transport: str) -> bool:
        """Return ``True`` when *transport* has a binding."""
        return str(transport or "").strip() in self._bindings

    def binding(self, transport: str) -> Optional[Binding]:
        """Return the full Binding or ``None``."""
        return self._bindings.get(str(transport or "").strip())

    @property
    def bindings(self) -> dict[str, Binding]:
        """Read-only view of all bindings."""
        return dict(self._bindings)

    def __len__(self) -> int:
        return len(self._bindings)

    def __contains__(self, transport: str) -> bool:
        return str(transport or "").strip() in self._bindings


# ----------------------------------------------------------------------
# Envelope formatting
# ----------------------------------------------------------------------

def _iso(ts: Any) -> str:
    """Normalise a timestamp to an ISO-8601 string."""
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.isoformat()
    if ts is None:
        return datetime.now(timezone.utc).isoformat()
    return str(ts)


def format_envelope(
    *,
    ts: Any = None,
    from_id: str,
    to_id: str,
    about: str,
    kind: str,
    msg_id: Optional[str] = None,
    transport: str = "direct",
    correlation_id: Optional[str] = None,
) -> str:
    """Format a single inter-agent event line.

    Layout (pipe-delimited, 7 fields)::

        ts | from_id -> to_id | about=<subject> | kind=<kind> | msg_id | transport | correlation_id

    ``correlation_id`` is ``-`` when omitted.
    ``kind`` must be one of :data:`VALID_KINDS`.
    """
    if kind not in VALID_KINDS:
        raise ValueError(f"kind {kind!r} not in VALID_KINDS")
    mid = msg_id or str(uuid.uuid4())
    corr = correlation_id or "-"
    return (
        f"{_iso(ts)} | {from_id} -> {to_id}"
        f" | about={about}"
        f" | kind={kind}"
        f" | {mid}"
        f" | {transport}"
        f" | {corr}"
    )


def append_event(
    path: str | Path,
    line: str,
) -> Path:
    """Append one envelope line to *path*, creating parent dirs as needed."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return p

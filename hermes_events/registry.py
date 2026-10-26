"""Event registry: declarative handler registration against absolute time.

The moment a timed event fires is expressed as ``when now() after <t>`` — the
processor evaluates exactly that predicate on every tick. ``<t>`` is always
absolute epoch; ``parse_when`` is registrar sugar so callers can say "in 1h".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

_REL_RE = re.compile(
    r"^\s*(?:через|in)?\s*(\d+(?:\.\d+)?)\s*"
    r"(s|сек|мин|m(in)?|ч|h(our)?|d(ay)?|д(ень)?|нед|w(eek)?)\s*$",
    re.IGNORECASE,
)
_UNIT_S = {"s": 1, "сек": 1, "m": 60, "min": 60, "мин": 60, "м": 60,
           "h": 3600, "hour": 3600, "ч": 3600, "час": 3600,
           "d": 86400, "day": 86400, "день": 86400, "д": 86400,
           "w": 604800, "week": 604800, "нед": 604800}


def parse_when(when: float | str, *, now_fn) -> float:
    """Resolve a registration moment to absolute epoch.

    Accepts epoch floats and relative sugar ("in 1h", "через 30 мин").
    Relative values are resolved AT REGISTRATION time by the registrar —
    the registry itself only ever stores absolute ``t``.
    """
    if isinstance(when, (int, float)):
        return float(when)
    m = _REL_RE.match(str(when))
    if not m:
        raise ValueError(f"cannot parse 'when': {when!r}")
    amount = float(m.group(1))
    unit = m.group(2).lower().rstrip(".")
    for key, mult in _UNIT_S.items():
        if unit.startswith(key):
            return now_fn() + amount * mult
    raise ValueError(f"unknown unit in 'when': {when!r}")


class EventKind(str, Enum):
    ONCE = "once"            # removed after execution (journal-traced)
    PERIODIC = "periodic"    # auto-rearm ONLY after completion
    SERVICE = "service"      # inline in tick body; never queued, never wakes LLM


@dataclass
class Event:
    id: str
    kind: EventKind
    t: float                       # absolute epoch; the <t> of `now() after <t>`
    handler: Callable[[float], Any]
    period_s: float | None = None  # PERIODIC only; measured from completion
    in_flight: bool = False        # executor token — guards the queue dedup
    wait_again: bool = False       # a re-fire happened while in flight
    meta: dict[str, Any] = field(default_factory=dict)


class Registry:
    """Thread-safe store of registered events. Registration is the only way
    in; the processor never invents handlers."""

    def __init__(self, clock) -> None:
        self._clock = clock
        self._events: dict[str, Event] = {}

    def register(
        self, *, id: str, kind: EventKind | str, when: float | str,
        handler: Callable[[float], Any], period_s: float | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Event:
        kind = EventKind(kind)
        if kind is EventKind.PERIODIC and not period_s:
            raise ValueError("periodic events need period_s")
        ev = Event(
            id=id, kind=kind, t=parse_when(when, now_fn=self._clock.now),
            handler=handler, period_s=period_s, meta=meta or {},
        )
        self._events[id] = ev
        return ev

    def unregister(self, id: str) -> bool:
        return self._events.pop(id, None) is not None

    def due(self, now_ts: float) -> list[Event]:
        """Events whose predicate ``now() after t`` holds. Firing decision
        (queue/mark) lives in the processor, not here."""
        return [e for e in self._events.values() if now_ts > e.t]

    def all(self) -> list[Event]:
        return list(self._events.values())

    def __len__(self) -> int:
        return len(self._events)

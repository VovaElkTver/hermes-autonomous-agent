"""Event registry: declarative handler registration against absolute time.

The moment a timed event fires is expressed as ``when now() after <t>`` — the
processor evaluates exactly that predicate on every tick. ``<t>`` is always
absolute epoch; ``parse_when`` is registrar sugar so callers can say "in 1h".
"""

from __future__ import annotations

import importlib
import json
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


def persistable_ref(handler: Callable) -> str | None:
    """``module:qualname`` for an importable handler, else None (closure/lambda)."""
    mod = getattr(handler, "__module__", None)
    qual = getattr(handler, "__qualname__", None)
    if not mod or not qual or "<" in qual:
        return None
    return f"{mod}:{qual}"


def resolve_ref(ref: str) -> Callable | None:
    """Import a stored ``module:qualname``; None when it cannot be loaded."""
    module_name, sep, attr = ref.partition(":")
    if not sep or not module_name:
        return None
    try:
        obj: Any = importlib.import_module(module_name)
    except Exception:
        return None
    for part in attr.split("."):
        obj = getattr(obj, part, None)
        if obj is None:
            return None
    return obj if callable(obj) else None


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
    handler_ref: str | None = None  # importable ref persisted in the separate DB
    meta: dict[str, Any] = field(default_factory=dict)


class Registry:
    """Store of registered events, optionally backed by the separate DB.

    Registration is the only way in; the processor never invents handlers.
    When a ``store`` is attached every state change is written through, so the
    DB mirrors the in-memory map at all times and ``load()`` can rebuild it
    after a restart.
    """

    def __init__(self, clock, store=None) -> None:
        self._clock = clock
        self._store = store
        self._events: dict[str, Event] = {}

    # -- write-through ----------------------------------------------------
    def _save(self, ev: Event) -> None:
        # only mirror live entries: an ONCE event was already deleted by
        # unregister() and must not resurrect itself on the completion path.
        if self._store is None or self._events.get(ev.id) is not ev:
            return
        self._store.upsert(
            id=ev.id, kind=ev.kind.value, t=ev.t, period_s=ev.period_s,
            handler_ref=ev.handler_ref or "", meta=ev.meta,
            in_flight=ev.in_flight, wait_again=ev.wait_again,
        )

    def register(
        self, *, id: str, kind: EventKind | str, when: float | str,
        handler: Callable[[float], Any], period_s: float | None = None,
        meta: dict[str, Any] | None = None, handler_ref: str | None = None,
    ) -> Event:
        kind = EventKind(kind)
        if kind is EventKind.PERIODIC and not period_s:
            raise ValueError("periodic events need period_s")
        ref = handler_ref or persistable_ref(handler)
        if self._store is not None and ref is None:
            raise ValueError(
                f"handler {handler!r} is not importable; pass handler_ref "
                "explicitly to survive a restart of the separate DB"
            )
        ev = Event(
            id=id, kind=kind, t=parse_when(when, now_fn=self._clock.now),
            handler=handler, period_s=period_s, meta=meta or {},
            handler_ref=ref,
        )
        self._events[id] = ev
        self._save(ev)
        return ev

    def unregister(self, id: str) -> bool:
        removed = self._events.pop(id, None) is not None
        if removed and self._store is not None:
            self._store.delete(id)
        return removed

    def save(self, ev: Event) -> None:
        """Public write-through for processor state changes (t/flags)."""
        self._save(ev)

    def get(self, id: str) -> Event | None:
        return self._events.get(id)

    # -- restart ----------------------------------------------------------
    def load(self) -> list[str]:
        """Rebuild the map from the DB. Returns notes for restored anomalies.

        At-least-once: a row left ``in_flight`` by a crash is normalized to a
        due event (flags cleared, ``t`` pulled to now) so the next tick
        re-enqueues it — neither dropped nor duplicated.
        """
        notes: list[str] = []
        if self._store is None:
            return notes
        now_ts = self._clock.now()
        for row in self._store.rows():
            handler = resolve_ref(row["handler_ref"])
            if handler is None:
                notes.append(
                    f"{row['id']}: handler '{row['handler_ref']}' unresolvable "
                    "— row kept in DB, not loaded"
                )
                continue
            ev = Event(
                id=row["id"], kind=EventKind(row["kind"]),
                t=row["t"], handler=handler, period_s=row["period_s"],
                in_flight=bool(row["in_flight"]),
                wait_again=bool(row["wait_again"]),
                handler_ref=row["handler_ref"],
                meta=json.loads(row["meta"] or "{}"),
            )
            if ev.in_flight:
                ev.in_flight = False
                ev.wait_again = False
                if not (ev.t < now_ts):
                    ev.t = now_ts
                notes.append(f"{ev.id}: re-armed after crash (at-least-once)")
            self._events[ev.id] = ev
            self._save(ev)
        return notes

    def due(self, now_ts: float) -> list[Event]:
        """Events whose predicate ``now() after t`` holds. Firing decision
        (queue/mark) lives in the processor, not here."""
        return [e for e in self._events.values() if now_ts > e.t]

    def all(self) -> list[Event]:
        return list(self._events.values())

    def __len__(self) -> int:
        return len(self._events)

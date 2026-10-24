"""controller.memory_gate — dispatch-time memory-pressure gate for worker spawns.

Incident 24.10 09:07: a worker spawn on a host already near OOM (ninfer-serve
at 67GB of 93GB) tipped the host over the edge. The kanban dispatcher carries
the live memory-pressure guard for this class (OOF-30/OOF-77,
``kanban_db_dispatch._system_memory_sample``); the pump gets the one-level
analogue: below the MemAvailable floor the worker spawn is DEFERRED — not
dropped, no failure, no run row, no step claim — and the tick journals
'deferred: memory pressure' so a sustained deferral is visible instead of a
silent queue. Mirrors the kanban pattern: same /proc-backed sample, same
fail-open on unknown (never brick the queue where /proc is unavailable).
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# MemAvailable floor below which a worker spawn is deferred. 8 GiB: a headless
# hermes worker is a full session (model context + tool subprocesses); spawning
# one with less than 8 GiB reclaimable left on the host risks an OOM that takes
# co-resident services down with it (the 24.10 09:07 incident shape).
DEFER_BELOW_KIB = 8 * 1024 * 1024

DEFERRED_REASON = "deferred: memory pressure"


def system_memory_sample() -> dict:
    """Best-effort ``{"mem_total_kib", "mem_available_kib", ...}`` snapshot,
    ``{}`` when unknown (non-Linux, read failure). The module attribute is the
    test seam — the same shape as kanban_db_dispatch._system_memory_sample."""
    try:
        from gateway.lifecycle_ledger import sample_memory
        return sample_memory() or {}
    except (ImportError, OSError):
        return {}


def pressure_reason(sample: dict | None = None) -> str | None:
    """``DEFERRED_REASON`` when MemAvailable is below the floor, else None.

    Fails open on an unknown or bogus sample: the gate must never be the thing
    that silences the queue on a host without /proc."""
    if sample is None:
        sample = system_memory_sample()
    avail = sample.get("mem_available_kib")
    if isinstance(avail, bool) or not isinstance(avail, int) or avail <= 0:
        return None
    if avail < DEFER_BELOW_KIB:
        return DEFERRED_REASON
    return None

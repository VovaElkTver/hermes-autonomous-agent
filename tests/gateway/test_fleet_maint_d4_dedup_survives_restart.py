"""Д4 integration: inbound event dedup must survive a gateway restart.

Incident (GEPA snapshot 28.11, nj node): the same ``platform_message_id`` written twice
into one state.db, gap 0 s — a restart/replay path re-admitted an already-processed
Matrix event. The Matrix adapter's dedup (``_is_duplicate_event``) is in-memory
(deque maxlen=1000), so a fresh process has no memory of what it already ingested.

Contract (ТЗ §3 P0): dedup on admission is mandatory and durable — the fix belongs on
the admission path (gateway/session_ingress.py is the one owner), keyed by the native
platform event id in the runtime store, not in adapter memory.
"""

import os
import sys
from collections import deque

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


def _bare_adapter():
    """A MatrixAdapter without __init__ — enough surface for the dedup method under test."""
    from plugins.platforms.matrix.adapter import MatrixAdapter

    adapter = MatrixAdapter.__new__(MatrixAdapter)
    adapter._processed_events = deque(maxlen=1000)
    adapter._processed_events_set = set()
    return adapter


def test_in_memory_dedup_within_one_process():
    """Green pin: current behavior inside one process is correct."""
    adapter = _bare_adapter()
    assert adapter._is_duplicate_event("$ev1") is False  # first sight: process it
    assert adapter._is_duplicate_event("$ev1") is True   # second sight: skip


@pytest.mark.xfail(strict=True, reason="Д4: dedup memory is per-process; after a restart the same "
                                       "event replays as new (incident: double write, gap 0 s)")
def test_dedup_survives_process_restart(tmp_path, monkeypatch):
    """A fresh adapter (simulating a restarted gateway) over the same HERMES_HOME must
    still recognize an event the previous process already admitted."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    first = _bare_adapter()
    assert first._is_duplicate_event("$ev_replayed") is False
    second = _bare_adapter()  # gateway bounce: no in-memory inheritance
    assert second._is_duplicate_event("$ev_replayed") is True

"""Д2 integration: a zombie gateway (loop frozen, process alive) self-escalates.

Incident 28.11: a gateway lived 9h15m without processing Matrix traffic; freshness
primitives existed but nothing consumed them into action inside the fork (external
watchdogs are forbidden by the owner regime, ТЗ §4.2).

Contract (ТЗ §5.2): staleness ≤10 min is detected and cured (self-restart request)
without losing queued inbound.
"""

import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


def test_write_loop_heartbeat_primitives(tmp_path, monkeypatch):
    """Green pin: the heartbeat primitive itself — atomic JSON with pid/updated_at."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from gateway.shutdown_watchdog import get_loop_heartbeat_path, write_loop_heartbeat

    path = write_loop_heartbeat(pid=4242, home=tmp_path)
    assert path == get_loop_heartbeat_path(tmp_path)
    payload = json.loads(path.read_text())
    assert payload["pid"] == 4242 and "updated_at" in payload


@pytest.mark.xfail(strict=True, reason="Д2: no in-fork consumer turns a stale loop heartbeat "
                                       "into a restart request (only external supervisors read it)")
def test_stale_loop_self_escalation_api_exists(tmp_path, monkeypatch):
    """An internal guard must expose staleness + action: a tick that, past the threshold,
    records ``restart_requested`` (gateway/run_shutdown.py degraded path) and leaves the
    durable inbound rows (state/gateway.heartbeat + runtime store queue) untouched."""
    from gateway import run_watchers

    guard = getattr(run_watchers, "_stale_loop_guard", None)
    assert callable(guard)


@pytest.mark.xfail(strict=True, reason="Д2: restart-without-queue-loss is untested end-to-end; "
                                       "acceptance requires a real cycle on a test state.db")
def test_self_restart_preserves_inbound_queue(tmp_path, monkeypatch):
    from gateway import run_watchers

    guard = getattr(run_watchers, "_stale_loop_guard", None)
    assert callable(guard)

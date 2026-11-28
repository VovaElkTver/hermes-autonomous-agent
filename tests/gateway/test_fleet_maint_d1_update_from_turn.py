"""Д1 integration: an update started from an active gateway turn must survive itself.

Incident 28.11 (Win): the agent ran ``hermes update`` from inside an active turn via the
terminal tool; the gateway pause the updater performs killed the very process that was
running the turn ("conversation is paused" / ``paused: unknown_execution``).

Contract (ТЗ §5.1): the initiating turn completes with a deterministic answer; the update
either runs detached (``--gateway`` hand-off) or the turn yields before the restart.

The ``/update`` slash path already satisfies this (``_spawn_detached_update``); the hole is
the *terminal*-spawned bare ``hermes update`` — which inherits ``_HERMES_GATEWAY=1`` from
the gateway (gateway/run.py stamps it) and must therefore hand off, not run in-process.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


def test_gateway_marker_is_the_turn_provenance():
    """The env marker distinguishing an in-turn child from an operator shell exists."""
    from hermes_cli.update_restart_recovery import _GATEWAY_MARKERS

    assert "_HERMES_GATEWAY" in _GATEWAY_MARKERS


@pytest.mark.xfail(strict=True, reason="Д1: bare `hermes update` from a gateway turn still runs "
                                       "the pause in-process and kills its own host (incident 28.11 Win)")
def test_bare_update_under_gateway_marker_hands_off_detached(monkeypatch):
    """A bare ``hermes update`` launched with ``_HERMES_GATEWAY=1`` must NOT proceed
    synchronously: it must re-exec detached as ``update --gateway`` (hand-off) or yield."""
    from hermes_cli import update_cmd

    handoff = getattr(update_cmd, "gateway_turn_handoff_argv", None)
    assert callable(handoff), "update_cmd exposes no gateway-turn hand-off decision"
    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    argv = handoff(base_argv=[sys.executable, "-m", "hermes_cli.main", "update"])
    assert argv and "--gateway" in list(argv)

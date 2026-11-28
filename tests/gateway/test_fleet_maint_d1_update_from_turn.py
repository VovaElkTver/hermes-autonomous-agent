"""Д1 integration: an update started from an active gateway turn must survive itself.

Incident 28.11 (Win): the agent ran ``hermes update`` from inside an active turn via the
terminal tool; the gateway pause the updater performs killed the very process that was
running the turn ("conversation is paused" / ``paused: unknown_execution``).

Contract (ТЗ §5.1): the initiating turn completes with a deterministic answer; the update
runs detached (``--gateway`` hand-off) before any pause step can reach the host.

The ``/update`` slash path already satisfied this (``_spawn_detached_update``); the hole was
the *terminal*-spawned bare ``hermes update`` — closed 28.11 by
``hermes_cli/update_cmd.py::gateway_turn_handoff_argv`` + the hand-off spawn in ``main.cmd_update``.
"""

import inspect
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


def test_gateway_marker_is_the_turn_provenance():
    """The env marker distinguishing an in-turn child from an operator shell exists."""
    from hermes_cli.update_restart_recovery import _GATEWAY_MARKERS

    assert "_HERMES_GATEWAY" in _GATEWAY_MARKERS


def test_bare_update_under_gateway_marker_hands_off_detached(monkeypatch):
    """Marker present, bare run: the decision is a detached ``--gateway`` argv."""
    from hermes_cli.update_cmd import gateway_turn_handoff_argv

    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    argv = gateway_turn_handoff_argv(base_argv=[sys.executable, "-m", "hermes_cli.main"])
    assert argv and "--gateway" in list(argv)
    assert argv[:3] == [sys.executable, "-m", "hermes_cli.main"]


def test_operator_shell_update_is_not_hijacked(monkeypatch):
    """No marker (operator shell): update proceeds in place."""
    from hermes_cli.update_cmd import gateway_turn_handoff_argv

    for marker in ("_HERMES_GATEWAY", "HERMES_GATEWAY", "HERMES_GATEWAY_MODE"):
        monkeypatch.delenv(marker, raising=False)
    assert gateway_turn_handoff_argv() is None


def test_handoff_wired_into_cmd_update():
    """The decision is invoked from ``cmd_update`` itself — regression pin against a
    refactor that keeps the helper but drops the call site."""
    from hermes_cli import main

    fn = getattr(main.cmd_update, "__wrapped__", main.cmd_update)
    src = inspect.getsource(fn)
    assert "gateway_turn_handoff_argv" in src and "_spawn_gateway_turn_handoff" in src

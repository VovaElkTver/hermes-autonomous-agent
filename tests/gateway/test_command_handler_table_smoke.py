"""Regression for the 10.11 incident: mechanical removals left a dead name in
_IDLE_COMMANDS ("suggestions" without _handle_suggestions_command) → the getattr
table raised on EVERY inbound message, gateway deaf (rescue anamnesis
20261110-rtx5090-suggestions-attribute-deafness; hotfix bed7e955657).

The smoke asserts both command tuples build their handler tables cleanly and
that every name resolves to a real bound method — drift between command-name
tuples and handler methods dies here instead of in prod admission.
"""
from __future__ import annotations

import threading


def _make_runner():
    """Minimal GatewayRunner: command tables live on the class, no state needed."""
    from gateway.run import GatewayRunner

    return GatewayRunner.__new__(GatewayRunner)


def test_command_handler_tables_build_cleanly():
    runner = _make_runner()
    plain = runner._command_handler_table(runner._PLAIN_COMMANDS)
    idle = runner._command_handler_table(runner._IDLE_COMMANDS)
    # every declared command resolved to a callable bound method
    for table in (plain, idle):
        assert table, "handler table empty"
        for name, handler in table.items():
            assert callable(handler), f"command {name!r} has no live handler"


def test_no_dead_names_in_command_tuples():
    """Drift detector: tuple names without _handle_*_command or alias must fail."""
    runner = _make_runner()
    missing = [
        name
        for name in (*runner._PLAIN_COMMANDS, *runner._IDLE_COMMANDS)
        if not hasattr(
            runner,
            runner._COMMAND_HANDLER_ALIASES.get(name, f"_handle_{name.replace('-', '_')}_command"),
        )
    ]
    assert not missing, f"dead commands in tables (no handler): {missing}"


def test_suggestions_absent_by_contract():
    """/suggestions was excised with scheduled storage spine — deliberately gone."""
    runner = _make_runner()
    assert "suggestions" not in runner._IDLE_COMMANDS
    assert "suggestions" not in runner._PLAIN_COMMANDS

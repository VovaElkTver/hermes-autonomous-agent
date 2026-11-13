"""Fork updates touch origin only: the upstream sync is an explicit opt-in.

Our fork's `hermes update` must never reach NousResearch (no remote add, no
fetch, no ff-merge) unless the operator sets HERMES_UPDATE_UPSTREAM_SYNC=1.
Pins the gate in `_sync_with_upstream_if_needed` without invoking git.
"""

from unittest.mock import patch

import pytest

from hermes_cli import update_cmd, update_cmd_git
from hermes_cli.update_cmd_git import _upstream_sync_requested


def _explode(*_a, **_kw):
    raise AssertionError("upstream must not be touched when the gate is off")


def test_gate_off_by_default_skips_everything(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("HERMES_UPDATE_UPSTREAM_SYNC", raising=False)
    with patch.object(update_cmd_git, "_has_upstream_remote", _explode), patch.object(
        update_cmd_git, "_offer_upstream_remote", _explode
    ):
        called = update_cmd._sync_with_upstream_if_needed([], tmp_path)
    assert called is False
    out = capsys.readouterr().out
    assert "HERMES_UPDATE_UPSTREAM_SYNC" in out
    assert "Fetching upstream" not in out


def test_gate_respects_explicit_opt_in_and_skips_via_marker(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_UPDATE_UPSTREAM_SYNC", "1")
    # Names lazily imported from update_cmd resolve there; _offer lives only in
    # update_cmd_git's globals (not re-exported by update_cmd).
    with patch.object(update_cmd, "_has_upstream_remote", return_value=False), patch.object(
        update_cmd, "_should_skip_upstream_prompt", return_value=True
    ), patch.object(update_cmd_git, "_offer_upstream_remote", _explode):
        called = update_cmd._sync_with_upstream_if_needed([], tmp_path)
    # The legacy skip path: marker present → decline branch → still no git calls.
    assert called is False
    out = capsys.readouterr().out
    assert "fork update touches origin only" not in out  # gate notice not printed past opt-in


def test_gate_accepts_truthy_spelling(monkeypatch):
    for value in ("1", "true", "YES", "on"):
        monkeypatch.setenv("HERMES_UPDATE_UPSTREAM_SYNC", value)
        assert _upstream_sync_requested() is True
    for value in ("", "0", "false", "no", "off", "junk"):
        monkeypatch.setenv("HERMES_UPDATE_UPSTREAM_SYNC", value)
        assert _upstream_sync_requested() is False

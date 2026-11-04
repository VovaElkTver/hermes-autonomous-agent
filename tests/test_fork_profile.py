"""Tests for fork_profile (autonomous_mode / administrative_mode)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from fork_profile import administrative_mode, autonomous_mode, effective


@pytest.fixture
def cfg_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME; config.yaml drives the flags live."""
    home = tmp_path / ".hermes"
    home.mkdir()

    def set_cfg(**top_level):
        lines = "\n".join(f"{k}: {json.dumps(v)}" for k, v in top_level.items())
        (home / "config.yaml").write_text(lines + "\n", encoding="utf-8")

    monkeypatch.setenv("HERMES_HOME", str(home))
    yield set_cfg


def test_no_config_means_off(cfg_home):
    assert autonomous_mode() is False
    assert administrative_mode() is False


def test_autonomous_flag_read_live(cfg_home):
    cfg_home(autonomous_mode=True)
    assert autonomous_mode() is True
    cfg_home(autonomous_mode=False)
    assert autonomous_mode() is False


def test_admin_nested_under_autonomous(cfg_home):
    cfg_home(administrative_mode=True)
    assert administrative_mode() is False  # parent off -> sub-mode never on
    cfg_home(autonomous_mode=True, administrative_mode=True)
    assert administrative_mode() is True


def test_explicit_beats_bundle_and_absent_falls_back(cfg_home):
    cfg_home(autonomous_mode=True)
    # bundle entry active under the mode
    assert effective("matrix", "auto_thread", explicit=None, upstream_default=True) is False
    # explicit setting outranks the bundle even when mode is on
    truthy = "true"
    assert effective("matrix", "auto_thread", explicit=truthy, upstream_default=False) is True
    # not in bundle: untouched passthrough of donor default
    assert effective("other", "knob", explicit=None, upstream_default="up") == "up"


def test_mode_off_keeps_upstream_defaults(cfg_home):
    cfg_home()
    assert effective("matrix", "auto_thread", explicit=None, upstream_default=True) is True


def test_broken_config_fails_open(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with patch("hermes_cli.config.load_config_readonly", side_effect=RuntimeError("boom")):
        assert autonomous_mode() is False
        assert effective("matrix", "auto_thread", explicit=None, upstream_default=True) is True

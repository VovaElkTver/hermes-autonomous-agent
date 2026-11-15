"""П1 (controller-claims-v1): ``controller.enabled=true`` together with the
plugin listed in ``plugins.disabled`` used to skip silently at DEBUG while the
operator believed managed autonomy was on. The gate must warn loudly."""
import logging

import pytest

from hermes_cli.plugins_discovery import (
    ManifestGate,
    controller_disabled_note,
    gate_manifest,
)
from hermes_cli.plugins_manifest import PluginManifest


@pytest.fixture()
def ctl_manifest() -> PluginManifest:
    return PluginManifest(name="controller", source="user")


@pytest.fixture()
def flag_on(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.plugins_discovery.controller_flag_on", lambda: True)


def test_disabled_controller_with_flag_on_warns(ctl_manifest, flag_on):
    gate = gate_manifest(ctl_manifest, {"controller"}, None)
    assert isinstance(gate, ManifestGate)
    assert gate.action == "placeholder"          # behavior unchanged: still skipped
    assert gate.error == "disabled via config"
    level, message, *args = gate.log
    assert level == logging.WARNING              # ...but loud, not DEBUG
    assert "plugins.disabled" in message
    assert args                                   # '%s' consumes the lookup key


def test_no_warning_when_flag_off(ctl_manifest, monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.plugins_discovery.controller_flag_on", lambda: False)
    gate = gate_manifest(ctl_manifest, {"controller"}, None)
    assert gate.action == "placeholder"
    assert gate.log[0] == logging.DEBUG           # plain skip, no contradiction


def test_no_warning_for_other_plugins(flag_on):
    m = PluginManifest(name="disk-cleanup", source="user")
    gate = gate_manifest(m, {"disk-cleanup"}, None)
    assert gate.action == "placeholder"
    assert gate.log[0] == logging.DEBUG


def test_note_helper_purity():
    assert controller_disabled_note({"other"}, {"other"}) is None
    assert controller_disabled_note({"controller"}, {"other"}) is None

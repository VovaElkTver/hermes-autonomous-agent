"""Slash-command config writes must land in the routed profile's config.yaml.

Regression for #87939 / #75684: the multiplexed inbound handler already runs
every slash handler inside ``_profile_runtime_scope`` (routed HERMES_HOME
override), but several handlers built their write path from the module
constant ``gateway.run._hermes_home`` — the LAUNCH home — so ``/reasoning
--global``, ``/fast``, ``/verbose``
and ``/footer`` persisted into the default profile's config.yaml. They now go
through ``_gateway_config_home()`` like the reads do.
"""

from __future__ import annotations

import pytest
import hermes_yaml as yaml

import gateway.run as gateway_run
from gateway.config import GatewayConfig
from gateway.run import GatewayRunner, _profile_runtime_scope


class _Runner(GatewayRunner):
    """Bare runner (no __init__): the dispatcher binds the routed runtime scope ONCE around every
    table handler (#119915), so drive the handlers through it rather than calling them directly."""

    def __init__(self):  # skip GatewayRunner.__init__: only the dispatcher + slash mixins are exercised
        self.config = GatewayConfig(multiplex_profiles=True)

    def _session_key_for_source(self, _source):
        return "k"

    def _evict_cached_agent(self, _session_key):
        pass

    def _resolve_profile_home_for_source(self, _source):
        return getattr(self, "routed_home", gateway_run._gateway_config_home())

    async def slash(self, name: str, args: str) -> str:
        """``/<name> <args>`` through the real dispatcher (ambient scope = the receiving bot's)."""
        event = _Event(args)
        handled, reply = await self._hm_dispatch_canonical_command(event, event.source, "k", name)
        assert handled
        return reply


class _Event:
    def __init__(self, args: str = ""):
        self._args = args
        self.source = object()

    def get_command_args(self) -> str:
        return self._args


@pytest.fixture
def homes(tmp_path, monkeypatch):
    default_home = tmp_path / "default"
    routed_home = tmp_path / "profiles" / "beta"
    default_home.mkdir()
    routed_home.mkdir(parents=True)
    (default_home / "config.yaml").write_text("agent:\n  reasoning_effort: medium\n")
    (routed_home / "config.yaml").write_text("agent:\n  reasoning_effort: none\n")
    monkeypatch.setattr(gateway_run, "_hermes_home", default_home)
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    return default_home, routed_home


@pytest.mark.asyncio
async def test_slash_config_writes_hit_routed_profile_and_leave_default_untouched(homes):
    default_home, routed_home = homes
    default_before = (default_home / "config.yaml").read_bytes()
    runner = _Runner()

    with _profile_runtime_scope(routed_home):
        assert runner._save_gateway_config_key("agent.reasoning_effort", "high")

    routed = yaml.safe_load((routed_home / "config.yaml").read_text())
    assert routed["agent"]["reasoning_effort"] == "high"
    assert (default_home / "config.yaml").read_bytes() == default_before

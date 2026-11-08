"""Root-flag gating of cron (Вл 09.11): «все наши придумки под отдельным корневым
конфиг флагом». autonomous_mode=true (fork default) ⇒ NO cron anywhere:
no toolset registration, no ticker in bootstrap, REST/webhook surfaces fail closed.
autonomous_mode=false → donor upstream parity."""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def fork_cfg(monkeypatch):
    holder = {"map": {}}
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: holder["map"], raising=False)
    return holder["map"]


def test_cron_gate_mirrors_root_flag(fork_cfg) -> None:
    from tools.fork_mode import cron_runtime_enabled
    assert cron_runtime_enabled() is False  # fork default: autonomous ON → cron OFF
    fork_cfg["autonomous_mode"] = False
    assert cron_runtime_enabled() is True   # donor parity posture


def _reload_cronjob_tools(monkeypatch):
    import tools.cronjob_tools as ct
    monkeypatch.setattr("tools.cronjob_tools", ct)  # keep ref for reload restore
    importlib.reload(ct)
    return ct


def test_toolset_registration_follows_root_flag(fork_cfg, monkeypatch) -> None:
    """Agent-visible surface contract, asserted through registry.dispatch:
    ON → cronjob_manage unknown to the agent; OFF (donor parity) → works."""
    import json

    from tools.registry import registry

    def _dispatch():
        return json.loads(registry.dispatch("cronjob_manage", {"action": "list"}))

    _reload_cronjob_tools(monkeypatch)
    result = _dispatch()
    assert "Unknown tool" in str(result.get("error"))

    fork_cfg["autonomous_mode"] = False
    _reload_cronjob_tools(monkeypatch)
    result = _dispatch()
    assert result.get("success") is True and "jobs" in result


def test_api_server_cron_surface_fails_closed_under_flag(fork_cfg, monkeypatch) -> None:
    """_CRON_AVAILABLE must follow the root flag so /api/jobs/* and /api/cron/fire
    fail closed in the autonomous posture."""
    import gateway.platforms.api_server as api

    fork_cfg.clear()  # autonomous default → cron unavailable
    mod = importlib.reload(api)
    assert mod._CRON_AVAILABLE is False

    fork_cfg["autonomous_mode"] = False
    mod = importlib.reload(api)
    try:
        assert mod._CRON_AVAILABLE is True
    finally:
        pass


def test_webhook_cron_trigger_refuses_under_flag(fork_cfg, monkeypatch) -> None:
    """The webhook cron_job route must never reach cron machinery while frozen."""
    from aiohttp import web as aio_web

    fork_cfg.clear()
    from gateway.platforms.webhook import WebhookAdapter
    method = WebhookAdapter._handle_cron_trigger
    # Minimal self stub: only the guard path runs before any platform attribute use.
    calls = {}

    class _Resp:
        def __init__(self, status):
            self.status = status

    monkeypatch.setattr(
        "gateway.platforms.webhook.web.json_response",
        lambda payload, status=200, headers=None: calls.setdefault("resp", _Resp(status)))
    self_stub = object()
    result = method(self_stub, prompt="p", route_config={"cron_job": "job-x"},
                    route_name="r", event_type="ping", delivery_id="d1")
    assert calls["resp"].status == 503 and result is not None
    assert isinstance(result, aio_web.Response | dict) or result is calls["resp"]

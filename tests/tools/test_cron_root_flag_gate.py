"""Root-flag gating of cron (Вл 09.11): «все наши придумки под отдельным корневым
конфиг флагом». autonomous_mode=true (fork default) ⇒ NO cron anywhere:
no toolset registration, no ticker in bootstrap, REST/webhook surfaces fail closed.
autonomous_mode=false → donor upstream parity — except cron itself, which was excised."""

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


def test_api_server_cron_surface_fails_closed_under_flag(fork_cfg, monkeypatch) -> None:
    """_CRON_AVAILABLE must follow the root flag so /api/jobs/* and /api/cron/fire
    fail closed in the autonomous posture."""
    import gateway.platforms.api_server as api

    # Cron was excised: the REST surface stays fail-closed in every posture.
    fork_cfg.clear()  # autonomous default
    mod = importlib.reload(api)
    assert mod._CRON_AVAILABLE is False

    fork_cfg["autonomous_mode"] = False  # donor-parity posture, still no cron package
    mod = importlib.reload(api)
    assert mod._CRON_AVAILABLE is False


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

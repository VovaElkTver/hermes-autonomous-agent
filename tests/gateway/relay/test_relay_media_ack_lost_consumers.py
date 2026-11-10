from __future__ import annotations

import asyncio
import threading
from unittest.mock import MagicMock, patch

import pytest

from gateway import relay
from gateway.config import Platform, PlatformConfig
from gateway.platform_registry import platform_registry
from tests.gateway.relay.fake_connector import FakeConnector, proxy_403

CHAT = "C0REPORTS"


@pytest.fixture
def live_relay(monkeypatch):
    """A provisioned, connected relay adapter on a gateway loop thread."""
    for k in ("GATEWAY_RELAY_ID", "GATEWAY_RELAY_SECRET", "GATEWAY_RELAY_DELIVERY_KEY", "GATEWAY_RELAY_ENDPOINT",
              "GATEWAY_RELAY_ROUTE_KEYS", "GATEWAY_RELAY_INSTANCE_ID", "GATEWAY_RELAY_WAKE_URL",
              "GATEWAY_RELAY_BOT_IDS", "GATEWAY_RELAY_IDP_TOKEN_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GATEWAY_RELAY_PLATFORMS", "slack")
    monkeypatch.setattr("gateway.run._load_gateway_config", dict, raising=False)
    monkeypatch.setattr("hermes_cli.auth.resolve_nous_access_token", lambda: "nas-token")
    monkeypatch.setattr("gateway.relay.media._UPLOAD_RETRY_DELAYS_S", (0.0, 0.0), raising=False)

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    def run(coro):
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=20)

    connector = FakeConnector()
    run(connector.start())
    connector.install_http(monkeypatch)
    monkeypatch.setenv("GATEWAY_RELAY_URL", connector.url)
    platform_registry.unregister("relay")
    assert relay.self_provision_relay() is True
    assert relay.register_relay_adapter()
    adapter = platform_registry.get("relay").adapter_factory(PlatformConfig(enabled=True))
    assert run(adapter.connect()) is True
    try:
        yield connector, adapter, loop
    finally:
        run(adapter.disconnect())
        run(connector.stop())
        platform_registry.unregister("relay")
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()


def _gateway_config():
    config = MagicMock()
    config.platforms = {Platform.RELAY: PlatformConfig(enabled=True)}
    config.get_home_channel = lambda p: None
    return config



@pytest.fixture(params=["ack_timeout", "socket_drop"])
def ack_lost_relay(request):
    """The connected relay, with every ``send_media`` applied but its ack lost:
    either it never comes back (timeout) or the socket drops right after the
    connector received the frame (reader exit settles the pending request)."""
    connector, adapter, loop = request.getfixturevalue("live_relay")
    if request.param == "ack_timeout":
        connector.unacked_ops = {"send_media"}
    else:
        connector.dropping_ops = {"send_media"}
    adapter._transport._outbound_timeout_s = 0.5
    return connector, adapter, loop


def _no_fallback_notice(connector) -> bool:
    return not any("q3-report" in str(a.get("content", "")) for a in connector.ops("send"))


def test_send_message_tool_reports_an_unconfirmed_attachment(ack_lost_relay, tmp_path):
    from tools.send_message_tool import _send_live_adapter_media

    connector, adapter, loop = ack_lost_relay
    pdf = tmp_path / "q3-report.pdf"
    pdf.write_bytes(b"%PDF-1.4 q3")

    result = asyncio.run_coroutine_threadsafe(
        _send_live_adapter_media(adapter, CHAT, "Q3 report", [(str(pdf), False)]), loop
    ).result(timeout=20)

    assert len(connector.ops("send_media")) == 1        # it went out once
    assert result["success"] is True                     # not a failure the model would retry
    assert "do not resend" in result["delivery_unconfirmed"]
    assert _no_fallback_notice(connector)


def test_cron_marks_an_unconfirmed_attachment_unverified(ack_lost_relay, tmp_path):
    from cron.jobs import create_job, get_job, mark_job_run
    from cron.scheduler import _deliver_result

    connector, adapter, loop = ack_lost_relay
    pdf = tmp_path / "q3-report.pdf"
    pdf.write_bytes(b"%PDF-1.4 q3")
    job = create_job("Post the Q3 report", "every 1d", name="Q3 report", deliver="origin",
                     origin={"platform": "slack", "chat_id": CHAT})
    with patch("gateway.config.load_gateway_config", return_value=_gateway_config()), \
         patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}), \
         patch("cron.scheduler_delivery._maybe_mirror_cron_delivery", lambda *a, **k: None):
        error = _deliver_result(job, f"Q3 numbers are in.\n\nMEDIA:{pdf}",
                                adapters={Platform.RELAY: adapter}, loop=loop)
    mark_job_run(job["id"], True, delivery_error=error)
    record = get_job(job["id"])
    assert record is not None

    assert len(connector.ops("send_media")) == 1
    assert error is None                                  # possibly delivered: not a failed run
    assert record["last_delivery_unverified"], record     # ...but not a confirmed one either
    assert any(CHAT in target for target in record["last_delivery_unverified"])
    assert _no_fallback_notice(connector)

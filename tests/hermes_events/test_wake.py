"""Floor-1 wake-seam tests: policy is data, delivery is an injected transport."""

import asyncio
import json
import sys
import threading
import types

import pytest

from hermes_events import wake


class _Ev:
    def __init__(self, id="e1", meta=None):
        self.id = id
        self.meta = meta if meta is not None else {}


@pytest.fixture(autouse=True)
def _clean_transport():
    wake.set_wake_transport(None)
    yield
    wake.set_wake_transport(None)


def test_no_wake_meta_is_silent():
    hits = []
    wake.set_wake_transport(lambda t, x, ts: hits.append(t))
    wake.wake_handler(_Ev(meta={}), 1.0)
    wake.wake_handler(_Ev(meta={"wake": {"text": "no chat_id"}}), 1.0)
    assert hits == []


def test_wake_meta_calls_transport_with_exact_target_and_text():
    hits = []
    wake.set_wake_transport(lambda t, x, ts: hits.append((t, x, ts)))
    target = {"platform": "matrix", "chat_id": "!room:elk", "thread_id": "$t"}
    wake.wake_handler(_Ev(meta={"wake": dict(target, text="вставай")}), 42.0)
    assert hits == [(target, "вставай", 42.0)]  # text travels separately; target is routing-only
    # transport got a copy, not the live meta dict
    assert hits[0][0] is not _Ev(meta={"wake": target}).meta["wake"]


def test_missing_transport_and_broken_transport_never_raise():
    wake.wake_handler(_Ev(meta={"wake": {"chat_id": "c"}}), 1.0)  # no transport: logged only
    def boom(t, x, ts):
        raise RuntimeError("delivery exploded")
    wake.set_wake_transport(boom)
    wake.wake_handler(_Ev(meta={"wake": {"chat_id": "c"}}), 1.0)  # must not propagate


def test_gateway_transport_routes_through_deliver_wake(monkeypatch):
    """install_gateway_transport resolves the live adapter and calls deliver_wake in the loop."""
    fake_session = types.ModuleType("gateway.session")
    fake_wake = types.ModuleType("gateway.wake")
    delivered = []

    class _Source:
        def __init__(self, **kw):
            self.kw = kw

    fake_session.SessionSource = _Source
    fake_wake.adapter_supports_push = lambda a: True

    async def deliver_wake(adapter, *, text, session_id="", source=None, **kw):
        delivered.append({"adapter": adapter, "text": text, "source": source})

    fake_wake.deliver_wake = deliver_wake
    monkeypatch.setitem(sys.modules, "gateway", types.ModuleType("gateway"))
    monkeypatch.setitem(sys.modules, "gateway.session", fake_session)
    monkeypatch.setitem(sys.modules, "gateway.wake", fake_wake)

    class _P:
        value = "matrix"

    adapter = object()
    runner = types.SimpleNamespace(adapters={_P(): adapter})

    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    try:
        transport = wake.install_gateway_transport(runner, loop)
        transport({"platform": "matrix", "chat_id": "!r:e", "chat_type": "group"}, "долг", 7.0)
        assert len(delivered) == 1
        assert delivered[0]["adapter"] is adapter
        assert delivered[0]["text"] == "долг"
        assert delivered[0]["source"].kw["chat_id"] == "!r:e"
        # unknown platform -> no delivery, no raise
        transport({"platform": "telegram", "chat_id": "x"}, "t", 8.0)
        assert len(delivered) == 1
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=2)

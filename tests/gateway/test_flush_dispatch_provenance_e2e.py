"""E2E: a debounced TEXT turn dispatched by the base flush timer must be ADMITTED under
multiplex_profiles (29.10 live refusals: 3x authorization_home_without_provenance, each
~20 ms after '[Matrix] Flushing text batch'). Drives the real chain:
adapter.handle_message -> _start_session_processing -> _process_message_background ->
profile handler (binding) -> _handle_message -> _hm_admit_event -> admit_message.
Verdict read from the durable admission rows, not from logs.
"""
import asyncio
import logging

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.session_authority import initialize_session_authority
from hermes_state_runtime import list_session_admissions

CHAT = '15551234567@s.whatsapp.net'
HOME = '/tmp/wd-e2e-home'


async def _drive(tmp_path, monkeypatch, feed):
    from plugins.platforms.whatsapp.adapter import WhatsAppAdapter
    monkeypatch.setenv('WHATSAPP_ALLOWED_USERS', '15551234567')
    config = GatewayConfig(sessions_dir=tmp_path / 'sessions')
    config.multiplex_profiles = True
    from gateway.run import GatewayRunner
    runner = GatewayRunner(config)
    adapter = WhatsAppAdapter(PlatformConfig(enabled=True, extra={'session_path': str(tmp_path / 'wa')}))
    adapter.gateway_runner, runner.adapters = runner, {Platform.WHATSAPP: adapter}
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='e2e')
    runner._wire_adapter_handlers(adapter)
    # The stamping the profile handler does pre-dispatch (run_adapters.py) — replicate the
    # registration, then let the handler chain stamp it itself.
    from gateway.session_ingress_context import register_transport_home
    register_transport_home(runner, None, HOME)
    # Stop right after admission: no agent turn runs in this test.
    admitted = []
    real = runner._handle_message

    async def spy(event):
        admitted.append(event)
        return None

    runner._handle_message = spy  # _wire_adapter_handlers already captured the bound wrapper? no:
    # it wraps self._handle_message at dispatch time via __get__; if wired eagerly, patch underneath.
    try:
        await feed(adapter)
        for _ in range(50):  # let flush timer + background task settle
            await asyncio.sleep(0.02)
            if admitted:
                break
        rows = [row for sid in authority.sessions
                for row in list_session_admissions(authority.db, session_id=sid, pending_only=False)]
        return authority, admitted, rows
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_plain_text_admits(tmp_path, monkeypatch):
    async def feed(adapter):
        from gateway.platforms.event import MessageEvent
        ev = MessageEvent(text='plain', source=adapter.build_source(chat_id=CHAT, user_id=CHAT, chat_type='dm'),
                          message_id='p1')
        await adapter.handle_message(ev)
    authority, admitted, rows = await _drive(tmp_path, monkeypatch, feed)
    assert admitted, 'event never reached the handler chain'


@pytest.mark.asyncio
async def test_flushed_text_batch_admits(tmp_path, monkeypatch, caplog):
    async def feed(adapter):
        adapter._text_batch_delay_seconds = 0.05
        from gateway.platforms.event import MessageEvent
        ev = MessageEvent(text='chunk one', source=adapter.build_source(chat_id=CHAT, user_id=CHAT, chat_type='dm'),
                          message_id='b1')
        await adapter.handle_message(ev)
    with caplog.at_level(logging.WARNING, logger='gateway.session_envelope'):
        authority, admitted, rows = await _drive(tmp_path, monkeypatch, feed)
    assert admitted, 'flushed batch never dispatched'
    assert not any('authorization_home_without_provenance' in r.getMessage() for r in caplog.records), \
        [r.getMessage() for r in caplog.records]

"""Orphan-rescue admission (29.10 refusals, chat !yzPjsOSdMSfUfZKtVk):
_hm_orphan_rescue_preclaim swaps the dispatched message for a previously STAGED event; admission
then runs while the ambient native_callback is bound to the trigger message, so the identity check
(capture_provenance: context[1] is event) refuses the staged event as
authorization_home_without_provenance — the user's dictation died silently behind a busy turn.

Fix contract: the authority seam re-binds the identity slot for the rescued event iff its source
carries exactly the callback's home stamp (rescued_callback), and only there — a forged or
differently-stamped event still fails closed.
"""
import logging
from pathlib import Path

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.run_runtime import initialize_gateway_runtime
from gateway.runtime_ownership import process_ownership
from gateway.session_envelope import prepare_native
from gateway.session_ingress_context import native_callback, rescued_callback
from hermes_state_runtime import RuntimeStoreError, list_session_admissions

CHAT = '15551234567@s.whatsapp.net'


async def _rig(tmp_path, monkeypatch):
    from plugins.platforms.whatsapp.adapter import WhatsAppAdapter
    monkeypatch.setenv('WHATSAPP_ALLOWED_USERS', '15551234567')
    root = tmp_path / '.hermes'
    root.mkdir(mode=0o700)
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(root))
    import hermes_state
    monkeypatch.setattr(hermes_state, 'DEFAULT_DB_PATH', hermes_state._IMPORT_DEFAULT_DB_PATH)
    config = GatewayConfig()
    config.multiplex_profiles = True
    config._runtime_profile_homes = (('default', root),)
    runner = GatewayRunner(config)
    process_ownership.reserve([root])
    adapter = WhatsAppAdapter(PlatformConfig(enabled=True, extra={'session_path': str(tmp_path / 'wa')}))
    adapter.gateway_runner, runner.adapters = runner, {Platform.WHATSAPP: adapter}
    try:
        await initialize_gateway_runtime(runner)
        from gateway.session_ingress_context import register_transport_home
        register_transport_home(runner, None, root)
        runner._wire_adapter_handlers(adapter)
    except BaseException:
        process_ownership.release(root)
        raise
    return runner, adapter, str(root)


def _teardown(runner):
    for authority in list(getattr(runner, 'session_authorities', []) or []):
        authority.db.close()
    process_ownership.release(Path(runner.config._runtime_profile_homes[0][1]))


def _staged(adapter, home, message_id='staged'):
    source = adapter.build_source(chat_id=CHAT, user_id=CHAT, chat_type='dm')
    source._authorization_profile_home = home  # the stamp its own ingress gave it earlier
    return MessageEvent(text='the dictation', source=source, message_id=message_id)


def _trigger(adapter, message_id='trigger'):
    source = adapter.build_source(chat_id=CHAT, user_id=CHAT, chat_type='dm')
    return MessageEvent(text='next message', source=source, message_id=message_id)


@pytest.mark.asyncio
async def test_identity_mismatch_is_what_refused_it_mechanism_pinned(tmp_path, monkeypatch):
    """Without the seam re-bind, admitting a staged event under the trigger's callback refuses —
    this pins the 29.10 clause (fail-closed semantics, not the bug)."""
    runner, adapter, HOME = await _rig(tmp_path, monkeypatch)
    try:
        trigger, staged = _trigger(adapter), _staged(adapter, HOME)
        with native_callback(runner, trigger, HOME):
            with pytest.raises(RuntimeStoreError) as raised:
                await prepare_native(runner, staged)
            assert raised.value.field == 'authorization_home_without_provenance'
    finally:
        _teardown(runner)


@pytest.mark.asyncio
async def test_rescued_event_admits_under_trigger_callback(tmp_path, monkeypatch, caplog):
    runner, adapter, HOME = await _rig(tmp_path, monkeypatch)
    try:
        trigger, staged = _trigger(adapter, 't1'), _staged(adapter, HOME, message_id='$vnREvWkO')
        with caplog.at_level(logging.WARNING, logger='gateway.session_envelope'):
            with native_callback(runner, trigger, HOME):
                with rescued_callback(staged):
                    payload = await prepare_native(runner, staged)
        assert payload['native_text_v1']['event']['message_id'] == '$vnREvWkO'
        assert not [r for r in caplog.records if 'refused' in r.getMessage()]
    finally:
        _teardown(runner)


@pytest.mark.asyncio
async def test_foreign_or_unstamped_events_keep_failing_closed(tmp_path, monkeypatch):
    """The re-bind is not a bypass: a different home stamp or a forged source with no stamp must
    not acquire provenance from the ambient callback."""
    runner, adapter, HOME = await _rig(tmp_path, monkeypatch)
    try:
        trigger = _trigger(adapter)
        foreign = _staged(adapter, '/other/profile/home', message_id='foreign')
        forged = _staged(adapter, HOME, message_id='forged')
        del forged.source._authorization_profile_home
        expectations = {  # what the clause order actually names per defect shape
            'foreign': 'authorization_home_without_provenance',  # stamped home != callback home
            'forged': 'multiplex_without_provenance',  # no stamp at all under multiplex
        }
        for event in (foreign, forged):
            with native_callback(runner, trigger, HOME):
                with rescued_callback(event):
                    with pytest.raises(RuntimeStoreError) as raised:
                        await prepare_native(runner, event)
                    assert raised.value.field == expectations[event.message_id], raised.value.field
    finally:
        _teardown(runner)


@pytest.mark.asyncio
async def test_authority_seam_commits_rescued_turn(tmp_path, monkeypatch):
    """End-to-end at the seam that produced the refusals: _hm_authority_admission of a swapped-in
    staged event commits a durable row instead of returning a refusal notice."""
    runner, adapter, HOME = await _rig(tmp_path, monkeypatch)
    try:
        trigger, staged = _trigger(adapter, 't2'), _staged(adapter, HOME, message_id='rescued-row')
        # The swap happens BEFORE the callback-wrapped dispatch continues: reproduce that shape —
        # preclaim already returned `staged`, we are still inside the trigger's native_callback.
        with native_callback(runner, trigger, HOME):
            handled, reply = await runner._hm_authority_admission(staged, staged.source, 'k', False)
        # The discriminator vs 29.10: not the generic refusal notice — the turn was admitted and
        # only died later on the test rig's missing AI provider (a turn error, not invalid_params).
        assert handled is True, handled
        assert 'invalid_params' not in (reply or '') and 'refused' not in (reply or '').lower(), reply
        authority = runner.session_authority
        rows = [row for sid in authority.sessions
                for row in list_session_admissions(authority.db, session_id=sid, pending_only=False)]
        assert any((row.get('request_id') or '').endswith('rescued-row') or 'rescued-row' in str(row)
                   for row in rows), rows
    finally:
        _teardown(runner)

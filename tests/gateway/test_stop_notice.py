"""User-stop recovery note.

A user ``/stop`` leaves the interrupted plan in the transcript, but the generic partial-failed
boundary the runner writes for it is a ``failed_turn`` row the context compressor strips from the
model's history — so the model keeps re-running the dead plan on the next message. This test pins
the fix: a user stop stages a one-shot, model-visible control note for the session's NEXT turn, and
a gateway restart/shutdown (the auto-continue path) does NOT.
"""
from types import SimpleNamespace

from agent.turn_failure_copy import USER_STOP_RECOVERY_NOTE
from gateway.stop_notice import is_user_stop, mark_stop_note, pop_stop_note
from hermes_state import SessionDB


def test_mark_pop_round_trip(tmp_path):
    db = SessionDB(db_path=tmp_path / 'state.db')
    with db:
        assert pop_stop_note(db, 's') == []
        mark_stop_note(db, 's')
        # consumed exactly once
        assert pop_stop_note(db, 's') == [USER_STOP_RECOVERY_NOTE]
        assert pop_stop_note(db, 's') == []


def test_empty_session_is_a_noop(tmp_path):
    db = SessionDB(db_path=tmp_path / 'state.db')
    with db:
        mark_stop_note(db, '')
        assert pop_stop_note(db, 'unrelated') == []


def test_is_user_stop_matches_user_stop_causes_only():
    from gateway.run import _INTERRUPT_REASON_RESET, _INTERRUPT_REASON_STOP
    # a user stop (or a /new /reset that latched a Stop) is a hard plan boundary
    assert is_user_stop({'result': {'interrupt_message': _INTERRUPT_REASON_STOP}}, 'interrupted') is True
    assert is_user_stop({'result': {'interrupt_message': _INTERRUPT_REASON_RESET}}, 'interrupted') is True
    # a gateway restart/shutdown auto-continues (resume_pending) — opposite semantics, not a user stop
    assert is_user_stop({'result': {'interrupt_message': 'Gateway restarting'}}, 'interrupted') is False
    assert is_user_stop({'result': {'interrupt_message': 'Gateway shutting down'}}, 'interrupted') is False
    # no recorded cause, or a non-interrupted outcome: not a user stop
    assert is_user_stop({'result': {}}, 'interrupted') is False
    assert is_user_stop({'result': {'interrupt_message': _INTERRUPT_REASON_STOP}}, 'completed') is False
    assert is_user_stop(None, 'interrupted') is False


def _settle(tmp_path, cause, name):
    """One claimed admission on a real SessionDB settled through the owner's worker-thread
    settlement with *cause* recorded. Returns (db, row)."""
    from gateway.session_authority import LiveSession, SessionAuthority
    from gateway.session_contract import SessionRef
    from gateway.session_settlement_recovery import commit_and_publish
    from hermes_state_runtime import admit_session_input, begin_runtime_epoch, claim_session_input

    db = SessionDB(db_path=tmp_path / f'{name}.db')
    db.create_session('s', source='test')
    epoch = begin_runtime_epoch(db, instance_id='owner')
    runner = SimpleNamespace(_draining=False, _running=True, _restart_requested=True)
    authority = SessionAuthority(runner, profile_id='owned', instance_id='owner', db=db, epoch=epoch)
    live = authority.sessions['s'] = LiveSession(None, 'route')
    ref = SessionRef('owned', 's')
    admit_session_input(db, epoch=epoch, principal_id='person', session_id='s', request_id='r',
                        payload={'text': 'please work'})
    row = claim_session_input(db, epoch=epoch, session_id='s')
    authority._stamp_claim(ref, live, row)
    result = {'final_response': '', 'messages': [], 'interrupted': True, 'completed': False}
    if cause:
        result['interrupt_message'] = cause
    settlement = {}
    with db:
        commit_and_publish(authority, live, ref, row, '', 'interrupted', {'result': result}, settlement)
    return db, row


def test_user_stop_settlement_stages_the_note(tmp_path):
    """The real defect: a user stop must stage the one-shot note for the next turn."""
    from gateway.run import _INTERRUPT_REASON_STOP
    db, row = _settle(tmp_path, _INTERRUPT_REASON_STOP, 'stop')
    sid = row['target_session_id']
    with db:
        assert pop_stop_note(db, sid) == [USER_STOP_RECOVERY_NOTE]
        assert pop_stop_note(db, sid) == []


def test_restart_stop_does_not_stage_the_note(tmp_path):
    """A gateway restart/shutdown auto-continues (resume_pending): no hard-boundary note."""
    db, row = _settle(tmp_path, 'Gateway restarting', 'restart')
    with db:
        assert pop_stop_note(db, row['target_session_id']) == []


def test_no_interruption_stages_no_note(tmp_path):
    db, row = _settle(tmp_path, None, 'plain')
    with db:
        assert pop_stop_note(db, row['target_session_id']) == []

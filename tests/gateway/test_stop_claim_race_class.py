"""Stop of a queued admission the drain claims between the read and the cancel commit stops the
claim instead of refusing ``stale_generation`` while it runs (N57 class: hosted RPC, scheduled)."""
import asyncio
from types import SimpleNamespace

import pytest

from hermes_state_runtime import claim_session_input, list_session_admissions


def _claim_after_read(authority, session_id):
    """The drain claims the head after the caller read it ``queued``, before the cancel commits."""
    receipt = authority.receipt

    async def claimed(actor, ref, admission_id):
        seen = await receipt(actor, ref, admission_id)
        claim_session_input(authority.db, epoch=authority.epoch, session_id=session_id)
        return seen
    authority.receipt = claimed


def _hosted(hosted_owner, tmp_path, monkeypatch):
    from gateway.hosted_room_driver import TaskIdentity
    from gateway.session_hosted_rpc import HostedRoomAuthorityRPC
    authority, loop, principal, agent = hosted_owner
    rpc = HostedRoomAuthorityRPC(authority, loop, room_id='room', member_id='member', profile='default',
                                 principal=principal, authorize=lambda *args: True)
    coords = dict(profile='default', source='bot_room')
    sid = rpc.create(**coords, title='Group: room')['session_id']
    rpc.submit(**coords, session_id=sid, prompt='input', task=TaskIdentity('room', 'task', 'thread', 'turn'),
               execution_generation=1, on_terminal=lambda receipt: None)
    _claim_after_read(authority, sid)
    reply = rpc.interrupt(**coords, session_id=sid, expected_task_id='task')
    [row] = list_session_admissions(authority.db, session_id=sid)
    return reply == {'interrupted': True, 'status': 'interrupted'} and row['status'] == 'started' and agent.interrupted


@pytest.mark.parametrize('surface', [_hosted], ids=['hosted_rpc_interrupt'])
def test_stop_of_a_queued_row_claimed_mid_stop_stops_the_claim(surface, hosted_owner, tmp_path, monkeypatch):
    assert surface(hosted_owner, tmp_path, monkeypatch), 'the claimed execution was not told to stop'

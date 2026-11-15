"""П9 (rescue-claims 14.11): the ambiguous-approval watcher.

Evidence from rtx5090 (request_id=2f00957…, 22:45:36): the button card send timed out in
a transport-deafness window, the runner logged "stays armed" and stopped — the prompt was
invisible, the session blocked, and nothing told the owner. The watcher must settle the
other half of "possibly delivered": late success arms the timeout notice, late failure
posts exactly one text /approve fallback (deduped against a late tap), a connector
decline posts nothing.
"""
import concurrent.futures
import types

import gateway.run  # collection-time import: the watcher's thread re-imports it under the home-I/O guard
from gateway.run_turn_runner_approval_watch import watch_ambiguous_approval


class _Res:
    def __init__(self, success=True, message_id=None, error=None):
        self.success, self.message_id, self.error = success, message_id, error


def _runner(adapter=None):
    ctx = types.SimpleNamespace(
        session_key="agent:main:matrix:dm:!x:y", _status_adapter=adapter,
        _status_chat_id="!room:y", _status_thread_metadata={"thread_id": "t"},
        _run_still_current=lambda: True)
    calls = {"scheduled": [], "sends": []}

    def _schedule(coro, _msg):
        calls["scheduled"].append(coro)
        if hasattr(coro, "close"):
            coro.close()  # nothing runs the real loop here; drop the pending-coroutine warning
        fut = concurrent.futures.Future()
        fut.set_result(_Res())
        return fut
    runner = types.SimpleNamespace(_ctx=ctx, _schedule=_schedule)
    return runner, calls


def _arm(monkeypatch, outcome, pending, notices, adapter=None):
    monkeypatch.setattr(gateway.run, "_approval_send_outcome", lambda fut, timeout: outcome)
    import tools.approval as ta
    monkeypatch.setattr(ta, "get_pending_gateway_approval", lambda sk: pending)
    import gateway.run_turn_runner_approval_settle as settle
    monkeypatch.setattr(settle, "register_timeout_notice",
                        lambda runner, data, *, command, card_message_id: notices.append(card_message_id))
    import gateway.run_turn_runner_approval_text as atext
    monkeypatch.setattr(atext, "approval_text_prompt",
                        lambda *a, **k: "!approve rq-1 <cmd>")
    fut = concurrent.futures.Future()  # pending at arm time, like the real timed-out send
    approval_data = {"request_id": "rq-1", "command": "rm -rf x", "description": "d"}
    return fut, approval_data


def test_late_success_arms_timeout_notice_with_card_id(monkeypatch):
    notices = []
    runner, calls = _runner(adapter=_Res())
    fut, data = _arm(monkeypatch, "sent", pending={"request_id": "rq-1"}, notices=notices)
    th = watch_ambiguous_approval(runner, fut, approval_data=data, cmd="c", desc="d", flags={})
    fut.set_result(_Res(message_id="card-1"))
    th.join(5)
    assert notices == ["card-1"]          # notice armed with the late card's id
    assert calls["sends"] == []           # and no fallback text


def test_late_failure_posts_one_text_fallback(monkeypatch):
    notices = []
    adapter_calls = []

    class _A:
        typed_command_prefix = "!"
        def send(self, chat_id, msg, metadata=None):
            adapter_calls.append((chat_id, msg))
            async def _done():
                return _Res(success=True)
            return _done()
    runner, calls = _runner(adapter=_A())
    fut, data = _arm(monkeypatch, "failed", pending={"request_id": "rq-1"}, notices=notices, adapter=_A())
    th = watch_ambiguous_approval(runner, fut, approval_data=data, cmd="c", desc="d", flags={})
    fut.set_result(_Res(message_id="card-1"))
    th.join(5)
    assert len(calls["scheduled"]) == 1                       # exactly one fallback send
    assert notices == [None]                                  # armed for the text path (no card)


def test_late_failure_after_resolution_posts_nothing(monkeypatch):
    notices = []
    runner, calls = _runner()
    fut, data = _arm(monkeypatch, "failed", pending=None, notices=notices)
    th = watch_ambiguous_approval(runner, fut, approval_data=data, cmd="c", desc="d", flags={})
    fut.set_result(_Res(message_id="card-1"))
    th.join(5)
    assert calls["scheduled"] == []
    assert notices == []


def test_connector_decline_never_falls_back_to_text(monkeypatch):
    notices = []
    runner, calls = _runner()
    fut, data = _arm(monkeypatch, "declined", pending={"request_id": "rq-1"}, notices=notices)
    th = watch_ambiguous_approval(runner, fut, approval_data=data, cmd="c", desc="d", flags={})
    fut.set_result(_Res(message_id="card-1"))
    th.join(5)
    assert calls["scheduled"] == []
    assert notices == []


def test_foreign_request_id_counts_as_resolved(monkeypatch):
    # A newer approval replaced ours in the queue — posting fallback text for the stale one
    # would double-prompt the chat.
    notices = []
    runner, calls = _runner()
    fut, data = _arm(monkeypatch, "failed", pending={"request_id": "rq-2"}, notices=notices)
    th = watch_ambiguous_approval(runner, fut, approval_data=data, cmd="c", desc="d", flags={})
    fut.set_result(_Res(message_id="card-1"))
    th.join(5)
    assert calls["scheduled"] == []


def test_bare_result_double_is_inert():
    runner, _calls = _runner()

    class _Bare:
        def result(self, timeout=None):
            return _Res()
    assert watch_ambiguous_approval(runner, _Bare(), approval_data={"request_id": "r"},
                                    cmd="c", desc="d", flags={}) is None

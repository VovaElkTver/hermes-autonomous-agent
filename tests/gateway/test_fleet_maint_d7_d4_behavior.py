"""Д7/Д4 product behavior: notice-typed service forms, bot admission filter, durable dedup.

Complements the source-contract tests (test_fleet_maint_d7_service_form_storm.py,
test_fleet_maint_d4_dedup_survives_restart.py) with executable behavior.
"""
import asyncio
import os
import sys
from collections import deque
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


def _bare_matrix_adapter(captured):
    from plugins.platforms.matrix.adapter import MatrixAdapter

    adapter = MatrixAdapter.__new__(MatrixAdapter)
    adapter.format_message = lambda text: text
    adapter.truncate_message = lambda text, *_a, **_k: [text]
    adapter.max_message_length = 4000
    adapter._apply_relation_metadata = lambda content, **kwargs: None
    adapter._bot_users = frozenset({"@peer:mx.test"})
    adapter._allow_room_mentions = False
    adapter._extract_outbound_mentions = lambda text: []
    adapter._has_outbound_room_mention = lambda text: False
    adapter._markdown_to_html = lambda text: text
    adapter._inject_outbound_mention_links = lambda text: text
    adapter._processed_events = deque(maxlen=1000)
    adapter._processed_events_set = set()

    async def _send(room_id, content):
        captured.append(content)
        return "$evt"
    adapter._send_room_message = _send
    return adapter


def test_send_notice_emits_m_notice_msgtype():
    captured = []
    adapter = _bare_matrix_adapter(captured)
    result = asyncio.run(adapter.send_notice("!room:test", "⏳ Your message is saved."))
    assert result.success
    assert captured[0]["msgtype"] == "m.notice"
    assert captured[0]["body"] == "⏳ Your message is saved."


def test_plain_send_stays_m_text():
    captured = []
    adapter = _bare_matrix_adapter(captured)
    asyncio.run(adapter.send("!room:test", "a real answer"))
    assert captured[0]["msgtype"] == "m.text"


def test_service_form_instance_selects_notice_without_metadata():
    from gateway.service_form import service_form
    captured = []
    adapter = _bare_matrix_adapter(captured)
    asyncio.run(adapter.send("!room:test", service_form("⏸ paused")))
    assert captured[0]["msgtype"] == "m.notice"


def _event(metadata=None, reply_expected=False, own=False, mid="$m1"):
    return SimpleNamespace(metadata=metadata or {}, reply_expected=reply_expected,
                           reply_to_is_own_message=own, internal=False, message_id=mid)


def test_unaddressed_peer_bot_message_never_reaches_the_authority():
    from gateway import session_ingress

    calls = []

    class Authority:
        async def admit_native(self, event):
            calls.append(event)
            raise AssertionError("must not be reached")

    out = asyncio.run(session_ingress.admit_message(
        Authority(), _event(metadata={"sender_is_bot": True})))
    assert out is None and calls == []


def test_addressed_or_own_reply_from_a_peer_bot_is_still_admitted():
    from gateway import session_ingress

    class Authority:
        def __init__(self):
            self.seen = []

        async def admit_native(self, event):
            self.seen.append(event)
            raise RuntimeError("stop right after the admission point")

    for event in (_event(metadata={"sender_is_bot": True}, reply_expected=True),
                  _event(metadata={"sender_is_bot": True}, own=True)):
        authority = Authority()
        try:
            asyncio.run(session_ingress.admit_message(authority, event))
        except RuntimeError:
            pass
        assert len(authority.seen) == 1, "addressed bot input must proceed into admission"


def test_dedup_ledger_prunes_stale_entries(tmp_path, monkeypatch):
    """A 14-day window: the ledger answers replays, not history archaeology."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    first = _bare_matrix_adapter([])
    assert first._is_duplicate_event("$old") is False
    first._seen_db_conn.execute(
        "UPDATE seen_events SET ts = ts - 20 * 86400 WHERE event_id = '$old'")
    first._seen_db_conn.commit()
    del first._seen_db_conn  # next call reconnects; simulate the next process

    second = _bare_matrix_adapter([])
    assert second._is_duplicate_event("$recent") is False  # its insert prunes the aged row
    assert second._is_duplicate_event("$old") is False     # pruned: a 20-day replay counts fresh
    third = _bare_matrix_adapter([])
    assert third._is_duplicate_event("$recent") is True    # the recent id survived to a new process

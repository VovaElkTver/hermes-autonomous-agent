"""M4 (TZ-memdoor-5090-v2): shadow message-gate — flag-off silence, judge
parsing, unavailable-is-not-negative, context window, journal shape."""
import json
import threading
import time

import pytest

from plugins.platforms.matrix import message_shadow as ms


@pytest.fixture()
def home(tmp_path, monkeypatch):
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    ms._history.clear()
    return tmp_path


@pytest.fixture()
def on(monkeypatch):
    monkeypatch.setattr(ms, "message_shadow_enabled", lambda: True)


@pytest.fixture()
def off(monkeypatch):
    monkeypatch.setattr(ms, "message_shadow_enabled", lambda: False)


def _lines(home):
    p = home / "logs" / "message_shadow.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def _drain():
    for t in threading.enumerate():
        if t.name.startswith("msg-shadow-"):
            t.join(3)


def test_off_is_silent(off, home):
    ms.note_message("!r", "@u", "hello")
    ms.observe_dropped(None, "!r", "$ev", "@u", "hello")
    assert ms._history == {}
    assert _lines(home) == []


def test_judge_parses_yes_no_unparsable(on, monkeypatch):
    class R:
        def __init__(self, payload):
            self.payload = payload
        def read(self):
            return json.dumps(self.payload).encode()
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    import urllib.request
    def fake(req, timeout=None):
        return R({"choices": [{"message": {"content": "YES"}}]})
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    assert ms.ask_judge([{"from": "a", "text": "b"}]) == (1.0, "ok")

    def fake_no(req, timeout=None):
        return R({"choices": [{"message": {"content": "no."}}]})
    monkeypatch.setattr(urllib.request, "urlopen", fake_no)
    assert ms.ask_judge([{"from": "a", "text": "b"}]) == (0.0, "ok")

    def fake_junk(req, timeout=None):
        return R({"choices": [{"message": {"content": "maybe"}}]})
    monkeypatch.setattr(urllib.request, "urlopen", fake_junk)
    assert ms.ask_judge([{"from": "a", "text": "b"}]) == (None, "unparsable")

    def boom(req, timeout=None):
        raise OSError("connection refused")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert ms.ask_judge([{"from": "a", "text": "b"}]) == (None, "unavailable")


def test_judge_reasoning_preamble_uses_last_verdict_token(on, monkeypatch):
    """L8 lesson (28.11): a reasoning preamble must not mask the verdict."""
    class R:
        def __init__(self, payload):
            self.payload = payload
        def read(self):
            return json.dumps(self.payload).encode()
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    import urllib.request

    def fake(req, timeout=None):
        return R({"choices": [{"message": {"content":
            "The user did not mention the bot and it is mid-conversation. "
            "On the other hand it could answer the question. So yes, the "
            "bot should reply."}}]})
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    assert ms.ask_judge([{"from": "a", "text": "b"}]) == (1.0, "ok")

    def fake_negated(req, timeout=None):
        return R({"choices": [{"message": {"content":
            "Should it reply? Yes, the question looks addressable — "
            "but the owner is clearly talking to another person. no."}}]})
    monkeypatch.setattr(urllib.request, "urlopen", fake_negated)
    assert ms.ask_judge([{"from": "a", "text": "b"}]) == (0.0, "ok")

    def fake_neither(req, timeout=None):
        return R({"choices": [{"message": {"content":
            "none of the options seems certain"}}]})
    monkeypatch.setattr(urllib.request, "urlopen", fake_neither)
    assert ms.ask_judge([{"from": "a", "text": "b"}]) == (None, "unparsable")


def test_drop_journals_would_and_never_invents_negative(on, home, monkeypatch):
    monkeypatch.setattr(ms, "ask_judge", lambda ctx: (1.0, "ok"))
    ms.observe_dropped(None, "!r", "$e1", "@u", "please help")
    _drain()
    line = _lines(home)[0]
    assert line["msg_id"] == "$e1" and line["p_reply"] == 1.0
    assert line["would"] is True and line["judge"] == "ok"

    monkeypatch.setattr(ms, "ask_judge", lambda ctx: (None, "unavailable"))
    ms.observe_dropped(None, "!r", "$e2", "@u", "noise")
    _drain()
    line = _lines(home)[1]
    assert line["p_reply"] is None and line["would"] is None
    assert line["judge"] == "unavailable"


def test_context_window_is_bounded(on, home):
    for i in range(12):
        ms.note_message("!r", "@u", f"m{i}")
    ctx = ms._context("!r", "last")
    assert len(ctx) == ms._CTX_WINDOW
    assert ctx[-1]["text"] == "last"

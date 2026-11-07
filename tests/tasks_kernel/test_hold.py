"""v0.1.2 /qlist /qhold /qresume: visibility + parking of the chat's queue."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import tasks_kernel as tk


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_STATE_DB", str(tmp_path / "state.db"))
    return str(tmp_path / "state.db")


@pytest.fixture()
def sent(monkeypatch):
    seen: list[str] = []

    def _fake(task: dict) -> bool:
        seen.append(task["title"])
        return True

    monkeypatch.setattr(tk, "deliver_fn", _fake)
    return seen


def _seed(db, titles):
    tk.create_batch(CHAT, [{"title": t} for t in titles], db_path=db)
    tk.start(CHAT, db_path=db)


CHAT = "!room:host"


def _ev():
    return SimpleNamespace(source=SimpleNamespace(chat_id=CHAT))


def _run(mixin_call, db):
    """Invoke a gateway handler with HERMES_STATE_DB pointed at the test db."""
    from gateway.run_busy import GatewayBusySessionMixin as M
    obj = object.__new__(M)
    return asyncio.run(getattr(obj, mixin_call)(_ev()))


def test_hold_roundtrip(db, sent):
    assert not tk.is_held(CHAT, db_path=db)
    assert tk.hold(CHAT, db_path=db) is True
    assert tk.is_held(CHAT, db_path=db)
    assert tk.hold(CHAT, db_path=db) is False          # already held (OR IGNORE)
    assert tk.resume(CHAT, db_path=db) is True
    assert not tk.is_held(CHAT, db_path=db)
    assert tk.resume(CHAT, db_path=db) is False        # nothing to lift


def test_bridge_ignores_acks_while_held(db, sent):
    from gateway.task_bridge import on_owner_message as bridge_advance
    _seed(db, ["a", "b"])
    tk.hold(CHAT, db_path=db)
    assert bridge_advance(CHAT, text="/next") is None
    states = {t["title"]: t["state"] for t in tk.status(CHAT, db_path=db)["tasks"]}
    assert states["a"] == tk.WAIT_HUMAN                # untouched by /next
    assert len(sent) == 1                              # no new delivery


def test_next_and_skip_report_pause(db, sent):
    _seed(db, ["a", "b"])
    tk.hold(CHAT, db_path=db)
    out = _run("_handle_next_command", db)
    assert "паузе" in out and "/qresume" in out
    out = _run("_handle_skip_command", db)
    assert "паузе" in out


def test_qhold_qresume_messages(db, sent):
    out = _run("_handle_qhold_command", db)
    assert "поставлена на паузу" in out
    out = _run("_handle_qhold_command", db)
    assert "уже на паузе" in out
    out = _run("_handle_qresume_command", db)
    assert "возобновлена" in out
    out = _run("_handle_qresume_command", db)
    assert "не была на паузе" in out


def test_qlist_lists_open_tasks_with_pause_marker(db, sent):
    _seed(db, ["a", "b"])
    tk.on_owner_message(CHAT, db_path=db)              # consume a → b waits_human
    out = _run("_handle_qlist_command", db)
    assert "b" in out and "wait_human" in out
    assert "пауза" not in out
    tk.hold(CHAT, db_path=db)
    out = _run("_handle_qlist_command", db)
    assert "(пауза)" in out


def test_qlist_empty(db, sent):
    out = _run("_handle_qlist_command", db)
    assert "Открытых задач нет" in out

"""Снятие draft'а (26.11, инцидент task-978fd86d56): пользовательская команда
«отмени задачу» на draft падала ValueError: illegal transition draft -> cancelled.
Машина разрешала cancel только из running-подобных состояний; у draft'а не было
пути наружу, кроме бесконечного defer/ready. Черновик — самое чистое место для
отмены (нет side-эффектов): дыра именно в пред-dispatch зоне."""
import pytest


@pytest.fixture()
def cdc(tmp_path):
    from controller import model
    conn = model.connect(tmp_path / "state.db")
    yield conn, model
    conn.close()


def test_draft_cancel_is_legal(cdc):
    from controller import model, transitions
    conn, _ = cdc
    tid = model.create_task(conn, owner="v", workdir="/w", goal="g")
    res = transitions.transition(conn, tid, "cancelled", basis="user cancel command")
    assert res["to"] == "cancelled"
    assert conn.execute("SELECT state FROM ctl_task WHERE task_id=?",
                        (tid,)).fetchone()["state"] == "cancelled"


def test_deferred_cancel_too(cdc):
    from controller import model, transitions
    conn, _ = cdc
    tid = model.create_task(conn, owner="v", workdir="/w", goal="g")
    transitions.transition(conn, tid, "deferred", basis="park")
    res = transitions.transition(conn, tid, "cancelled", basis="user cancel command")
    assert res["to"] == "cancelled"


def test_commands_apply_cancel_on_draft_returns_ok(cdc):
    from controller import commands, model
    conn, _ = cdc
    tid = model.create_task(conn, owner="v", workdir="/w", goal="g")
    out = commands.apply(conn, {"op": "cancel", "arg": tid, "text": ""}, scope="/w")
    assert out["ok"] is True, out
    assert conn.execute("SELECT state FROM ctl_task WHERE task_id=?",
                        (tid,)).fetchone()["state"] == "cancelled"

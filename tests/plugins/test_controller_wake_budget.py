"""Wake seam + real budgets (go Вл 09.11 по разбора критика 09.11).

Покрывает три дефекта, найденных разбором:
- limit_value в ctl_budget_counter не писался НИКЕМ -> budget_exhausted был
  тождественно False (страж без содержания);
- контрактовский budgets так и не становился лимитом на ready->running;
- pump был привязан к due-счётчику: внешнее событие ждало до 300 с, вопреки
  доктрине «latency — политика due, счётчик над битом 1 Гц».
"""
import threading
import time

import pytest

from controller import model, transitions, wake


@pytest.fixture()
def env(tmp_path, monkeypatch):
    c = model.connect(tmp_path / "c.db")
    monkeypatch.setenv("HERMES_CONTROLLER_WAKE", str(tmp_path / "wake.seq"))
    yield c, tmp_path
    c.close()


def _ready_task(c, wd="/w", budgets=None):
    tid = model.create_task(c, owner="v", workdir=wd, goal="g")
    model.set_permission(c, wd, enabled=True, granted_via="user-command")
    if budgets:
        import json
        row = c.execute("SELECT fields FROM ctl_contract_revision"
                        " WHERE task_id=? AND version=1", (tid,)).fetchone()
        f = json.loads(row["fields"])
        f["budgets"] = budgets
        c.execute("UPDATE ctl_contract_revision SET fields=? WHERE task_id=? AND version=1",
                  (json.dumps(f), tid))
        c.commit()
    model.agree_contract(c, tid)
    c.execute("UPDATE ctl_task SET state='ready' WHERE task_id=?", (tid,))
    c.commit()
    return tid


# ── B: budgets ───────────────────────────────────────────────────────────────

def test_set_budget_makes_the_guard_real(env):
    c, _ = env
    tid = _ready_task(c)
    assert model.budget_exhausted(c, tid, "tool_calls") is False   # no row: legacy NULL world
    model.add_budget(c, tid, "tool_calls", 3)
    assert model.budget_exhausted(c, tid, "tool_calls") is False   # limit NULL -> never True
    model.set_budget(c, tid, "tool_calls", 2)
    assert model.budget_exhausted(c, tid, "tool_calls") is True    # the cap is alive now
    row = c.execute("SELECT used FROM ctl_budget_counter WHERE task_id=? AND metric='tool_calls'",
                    (tid,)).fetchone()
    assert row["used"] == 3  # setting a cap never rewinds consumption


def test_contract_budgets_apply_on_entering_running(env):
    c, _ = env
    tid = _ready_task(c, budgets={"tool_calls": 5, "tokens": "nonsense"})
    perm = model.get_permission(c, "/w")
    transitions.transition(c, tid, "running", basis="t", permission=perm)
    got = {r["metric"]: r["limit_value"] for r in
           c.execute("SELECT metric, limit_value FROM ctl_budget_counter WHERE task_id=?",
                     (tid,)).fetchall()}
    assert got.get("tool_calls") == 5
    assert "tokens" not in got  # non-numeric limit skipped, not fatal


# ── C: wake seam ─────────────────────────────────────────────────────────────

def test_notify_advances_the_seq(env):
    _c, _tmp = env
    assert wake.read_seq() == ""
    assert wake.notify() is True
    s1 = wake.read_seq()
    assert s1
    time.sleep(0.001)
    assert wake.notify() is True
    assert wake.read_seq() != s1


def test_transition_rings_the_bell(env):
    c, _ = env
    tid = _ready_task(c)
    perm = model.get_permission(c, "/w")
    transitions.transition(c, tid, "running", basis="t", permission=perm)
    assert wake.read_seq() != ""


def test_followup_add_rings_the_bell(env):
    from controller import followups
    c, _ = env
    followups.add(c, kind="reminder", scope_task="task-x", text="ping",
                  due_at=time.time() + 0.2)
    assert wake.read_seq() != ""


def test_wake_pulls_pump_forward(env):
    """pump с интервалом 3600 не успел бы сам; звонок должен вызвать drain на
    следующем бите — это и есть «latency = политика due» (п.1 критика)."""
    from controller import ticker
    c, _ = env
    t = ticker.Ticker(intervals={"pump": 3600, "watchdog": 3600, "followups": 3600},
                      gates_path=None, tick_s=0.02)
    fired = []
    t.run_pump = lambda conn: fired.append(1)
    t._owner_refresh = lambda conn: None
    th = threading.Thread(target=t._schedule, args=(c,), daemon=True)
    th.start()
    try:
        time.sleep(0.1)
        assert not fired, "pump fired without due and without wake"
        wake.notify()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not fired:
            time.sleep(0.02)
        assert fired, "wake file did not pull the pump to the current beat"
    finally:
        t._stop.set()
        th.join(3)

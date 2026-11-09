"""Run-progress telemetry: primary sink state.db (ctl_event kind=run_observation),
mirror agency-ledger `trace` — решение Вл 09.11 «фиксируем и делаем».

Run context comes from the controller worker spawn env (controller/workers.py):
HERMES_CONTROLLER_{RUN,TASK,STEP}_ID. Interactive sessions (no env) keep the
original Inspector-trace behaviour: agency-ledger only.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

TOOLSET_STORE = Path.home() / "projects" / "hermes-agency-toolset" / "plugins" / "agency" / "store.py"

pytestmark = pytest.mark.skipif(
    not TOOLSET_STORE.is_file(), reason="agency-toolset checkout not present")


def _load_real_store():
    spec = importlib.util.spec_from_file_location("agency_store_under_test", TOOLSET_STORE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def sinks(tmp_path, monkeypatch):
    """Sandboxed HERMES_HOME + real toolset store + flag on."""
    tt = importlib.import_module("tools.tool_trace")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    store = _load_real_store()
    db = str(home / "agency-state.db")
    store.seed(db)
    tt._store_cache = store
    tt.tool_trace_enabled = lambda: True
    return store, tt, home


def _ctl_events(home, kind="run_observation"):
    from controller import model as ctl_model
    conn = ctl_model.connect()
    rows = conn.execute("SELECT * FROM ctl_event WHERE kind=? ORDER BY ts", (kind,)).fetchall()
    return [dict(r) for r in rows]


def _agency_rows(store, home):
    conn = store.connect(str(home / "agency-state.db"))
    return [dict(r) for r in conn.execute(
        "SELECT seq,payload_json FROM ledger WHERE issue_id='trace' ORDER BY seq")]


def test_run_ctx_primary_and_mirror(sinks, monkeypatch):
    store, tt, home = sinks
    monkeypatch.setenv("HERMES_CONTROLLER_RUN_ID", "run-abc")
    monkeypatch.setenv("HERMES_CONTROLLER_TASK_ID", "task-xyz")
    monkeypatch.setenv("HERMES_CONTROLLER_STEP_ID", "step-007")
    tt.emit_tool_call(session_id="s1", name="probe", toolset="test",
                      args={"q": 1}, result={"r": 2}, ok=True, db_path=None)
    # primary: ctl_event in state.db
    ev = _ctl_events(home)
    assert len(ev) == 1 and ev[0]["origin"] == "tool_trace"
    payload = json.loads(ev[0]["payload"])
    assert payload["run_id"] == "run-abc" and payload["step_id"] == "step-007"
    assert payload["ok"] is True and "error_code" not in payload
    assert ev[0]["task_id"] == "task-xyz"
    # mirror: agency ledger got the same enriched event
    rows = _agency_rows(store, home)
    assert len(rows) == 1
    assert json.loads(rows[0]["payload_json"])["run_id"] == "run-abc"


def test_no_ctx_state_db_untouched_agency_only(sinks, monkeypatch):
    store, tt, home = sinks
    monkeypatch.delenv("HERMES_CONTROLLER_RUN_ID", raising=False)
    tt.emit_tool_call(session_id="s2", name="probe", toolset="test",
                      args={}, result={}, ok=True, db_path=None)
    assert _ctl_events(home) == []            # state.db untouched outside runs
    assert len(_agency_rows(store, home)) == 1  # original TZ behaviour intact


def test_primary_crash_never_breaks_mirror_or_tool(sinks, monkeypatch):
    store, tt, home = sinks
    monkeypatch.setenv("HERMES_CONTROLLER_RUN_ID", "run-broken")

    def boom(*a, **k):
        raise RuntimeError("state.db wedged")
    import controller.model as ctl_model
    monkeypatch.setattr(ctl_model, "emit", boom)

    out = tt.emit_tool_call(session_id=None, name="probe", toolset="test",
                            args={}, result={}, ok=True, db_path=str(home / "agency-state.db"))
    assert out is None                          # emit never raises
    assert len(_agency_rows(store, home)) == 1  # mirror still landed


def test_flag_off_writes_nothing(sinks, monkeypatch):
    store, tt, home = sinks
    monkeypatch.setenv("HERMES_CONTROLLER_RUN_ID", "run-off")
    tt.tool_trace_enabled = lambda: False
    tt.emit_tool_call(session_id=None, name="p", toolset="t", args={},
                      result={}, ok=True, db_path=None)
    assert _ctl_events(home) == [] and _agency_rows(store, home) == []

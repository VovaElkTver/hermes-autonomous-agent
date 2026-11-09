"""Inspector-trace emitter acceptance suite (ТЗ Вл 09.11, приёмка a–e).

Runs against the REAL agency-toolset store contract (submit() envelope,
BEGIN IMMEDIATE, monotonic seq), loaded by file path from
~/projects/hermes-agency-toolset/plugins/agency/store.py — no mock receiver.
"""
from __future__ import annotations

import importlib.util
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

TOOLSET_STORE = Path.home() / "projects" / "hermes-agency-toolset" / "plugins" / "agency" / "store.py"

pytestmark = pytest.mark.skipif(
    not TOOLSET_STORE.is_file(), reason="agency-toolset checkout not present")

# arg value that must NEVER appear verbatim in any ledger row (acceptance d)
SECRET = "SECRETPAYLOAD-do-not-leak-4311"


def _load_real_store():
    spec = importlib.util.spec_from_file_location("agency_store_under_test", TOOLSET_STORE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def store_mod(tmp_path, monkeypatch):
    """Real toolset store seeded against a tmp agency-state.db.

    HERMES_HOME is redirected for the whole test so registry-level dispatches
    (which pass db_path=None) land in the sandbox, not the agent's real home.
    """
    tt = importlib.import_module("tools.tool_trace")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    store = _load_real_store()
    db = str(home / "agency-state.db")
    store.seed(db)
    tt._store_cache = store          # bypass dormant loader; contract-real receiver
    tt.tool_trace_enabled = lambda: bool(tt._fixture_flag)  # pragma: no cover
    yield store, tt, db


@pytest.fixture()
def flag_on(store_mod):
    store, tt, db = store_mod
    tt._fixture_flag = True
    return store, tt, db


@pytest.fixture()
def flag_off(store_mod):
    store, tt, db = store_mod
    tt._fixture_flag = False
    return store, tt, db


def _rows(store, db):
    return store.connect(db).execute(
        "SELECT seq,type,kind,issue_id,actor_position_id,payload_json,idem_key"
        " FROM ledger ORDER BY seq").fetchall()


# --------------------------------------------------------------- acceptance a --
def test_flag_off_zero_events_zero_writes(flag_off):
    store, tt, db = flag_off
    before = len(_rows(store, db))
    tt.emit_tool_call(session_id="s-off", name="probe", toolset="test",
                      args={"x": SECRET}, result={"y": 1}, ok=True, db_path=db)
    assert len(_rows(store, db)) == before  # ни одного события, оверхед нулевой


# --------------------------------------------------------------- acceptance b --
def test_flag_on_exactly_one_event_deterministic_idem_key(flag_on):
    store, tt, db = flag_on
    call = dict(session_id="s-b", name="probe", toolset="test",
                args={"q": 1}, result={"r": 2}, ok=True)
    tt.emit_tool_call(**call, db_path=db)
    tt.emit_tool_call(**call, db_path=db)  # дубль-эмиссия той же логической работы
    rows = [r for r in _rows(store, db) if r["issue_id"] == "trace"]
    assert len(rows) == 1                  # дубль-эмит невозможен
    key = rows[0]["idem_key"]
    # детерминизм: тот же вызов из нового процесса/кэша даёт тот же ключ
    tt._store_cache = _load_real_store()
    tt.emit_tool_call(**call, db_path=db)
    rows = [r for r in _rows(store, db) if r["issue_id"] == "trace"]
    assert len(rows) == 1 and rows[0]["idem_key"] == key


# --------------------------------------------------------------- acceptance c --
def test_tracer_crash_does_not_change_tool_answer(store_mod, monkeypatch):
    store, tt, _db = store_mod
    tt._fixture_flag = True

    def boom(*a, **k):
        raise RuntimeError("tracer timeout/crash")
    monkeypatch.setattr(store, "submit", boom)

    from tools.registry import registry
    pname = "_trace_crash_probe"

    def handler(args):
        return json.dumps({"echo": args.get("msg", "")}, ensure_ascii=False)

    registry.register(pname, "test", {"parameters": {"type": "object"}}, handler,
                      description="trace crash probe", override=True)
    out = registry.dispatch(pname, {"msg": "alive"})
    assert json.loads(out)["echo"] == "alive"   # ответ тула не изменился и ход не прерван


# --------------------------------------------------------------- acceptance d --
def test_no_raw_payload_strings_in_event(flag_on):
    store, tt, db = flag_on
    tt.emit_tool_call(session_id="s-d", name="probe", toolset="test",
                      args={"token": SECRET}, result={"leakme": SECRET},
                      ok=True, db_path=db)
    rows = _rows(store, db)
    assert rows
    blob = json.dumps([dict(r) for r in rows], ensure_ascii=False)
    assert SECRET not in blob              # только дайджесты, сырых строк нет


# --------------------------------------------------------------- acceptance e --
def test_parallel_calls_both_land_seq_monotonic(flag_on):
    store, tt, db = flag_on

    def fire(i):
        tt.emit_tool_call(session_id=f"s-e{i}", name="probe", toolset="test",
                          args={"i": i}, result={"i": i}, ok=True, db_path=db)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(fire, range(2)))
    rows = [dict(r) for r in _rows(store, db) if r["issue_id"] == "trace"]
    assert len(rows) == 2
    seqs = [r["seq"] for r in rows]
    assert seqs == sorted(seqs) and len(set(seqs)) == 2   # монотонный seq


# ------------------------------------------ dispatch-level guard (single hook) --
def test_dispatch_success_and_error_paths_emit_once(store_mod, monkeypatch):
    store, tt, db = store_mod
    tt._fixture_flag = True

    from tools.registry import registry
    pname = "_trace_path_probe"

    def handler(args):
        if args.get("fail"):
            raise ValueError("boom")
        return json.dumps({"ok_msg": args["msg"]}, ensure_ascii=False)

    registry.register(pname, "test", {"parameters": {"type": "object"}}, handler,
                      description="trace paths probe", override=True)

    good = registry.dispatch(pname, {"msg": SECRET}, session_id="s-disp")
    assert "ok_msg" in json.loads(str(good))
    bad = registry.dispatch(pname, {"fail": True}, session_id="s-disp")
    assert "error" in json.loads(str(bad))    # tool_error-контракт (json-строка)

    rows = [json.loads(r["payload_json"]) for r in _rows(store, db)
            if r["issue_id"] == "trace" and r["type"] == "tool_call"]
    assert len(rows) == 2                              # ровно одно событие на вызов
    ok_ev, err_ev = rows[0], rows[1]
    assert ok_ev["ok"] is True and "SECRETPAYLOAD" not in json.dumps(ok_ev)
    assert err_ev["ok"] is False and err_ev["error_code"] == "ValueError"
    assert err_ev["tool"].endswith(pname) and err_ev["session"] == "s-disp"


def test_error_code_carries_exception_class(flag_on):
    store, tt, db = flag_on
    tt.emit_tool_call(session_id=None, name="probe", toolset="test",
                      args={}, ok=False, error_code="TimeoutError", db_path=db)
    ev = json.loads(_rows(store, db)[-1]["payload_json"])
    assert ev["ok"] is False and ev["error_code"] == "TimeoutError"
    assert ev["session"] == "-"                        # вне агентской сессии/без сессии

"""fork-guardrails v0.6.1 «арбитраж сервисов» (ToDo «Про́риоцепция» §1, Вл 21.11).

Регрессия на инцидент 21.11: ветка grpo-smoke остановила чужой serving :8006 без
согласования — лежал 45 минут. Механизм (класс ОС, не промпт): мутающая операция
над systemd-юнитом (start/stop/restart/reload/kill) минтит lease на юнит;
чужая сессия блокируется с вердиктом и именем владельца; stop владельцем
освобождает юнит (очередь: следующий первый пришедший берёт чистый юнит).
Владелец и следы — в lease-реестре плагина (resource "unit:<name>", имя сессии
из v0.6.0).
"""
import importlib.util
import json
import sqlite3
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _mk_state_db(path, rows):
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, title_source TEXT)")
    con.executemany("INSERT INTO sessions VALUES (?, ?, ?)", rows)
    con.commit()
    con.close()


def _load_plugin(tmp_path, state_db, **over):
    spec = importlib.util.spec_from_file_location(
        "fg_svc_under_test", REPO / "plugins" / "fork-guardrails" / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    settings = {"enabled": True, "log_only": False, "features": {},
                "audit_log": str(tmp_path / "fg" / "audit.jsonl"),
                "state_db": str(state_db)}

    class _Ctx:
        @staticmethod
        def get_config(key, fallback=None):
            return settings.get(key, fallback)

    mod._ctx = _Ctx()
    mod._REPO_CACHE.clear()
    return mod


def _term(mod, session_id, command, turn_id="t1"):
    return mod.pre_tool_call(tool_name="terminal", args={"command": command},
                             task_id="k", session_id=session_id, tool_call_id="c",
                             turn_id=turn_id, api_request_id="a", middleware_trace=[])


def _leases(mod, tmp_path):
    d = tmp_path / "fg" / "leases"
    out = []
    if d.exists():
        for f in d.glob("*.json"):
            rec = json.loads(f.read_text())
            rec["_file"] = f.name
            out.append(rec)
    return out


START = "systemctl --user start hermes-gpu-schedule-27b.service"
STOP = "systemctl --user stop hermes-gpu-schedule-27b.service"


def test_owner_start_mints_unit_lease(tmp_path):
    db = tmp_path / "state.db"
    _mk_state_db(db, [("s1", "trek0-serving", "user"), ("s2", "grpo-smoke", "user")])
    mod = _load_plugin(tmp_path, db)
    res = _term(mod, "s1", START)
    assert not (isinstance(res, dict) and res.get("action") == "block"), res
    leases = [l for l in _leases(mod, tmp_path) if l.get("resource") == "unit:hermes-gpu-schedule-27b.service"]
    assert leases and leases[0]["name"] == "trek0-serving", leases


def test_foreign_stop_is_blocked_with_holder_name(tmp_path):
    db = tmp_path / "state.db"
    _mk_state_db(db, [("s1", "trek0-serving", "user"), ("s2", "grpo-smoke", "user")])
    mod = _load_plugin(tmp_path, db)
    _term(mod, "s1", START)
    res = _term(mod, "s2", STOP)
    blob = json.dumps(res, default=str)
    assert res and '"block"' in blob, res
    assert "trek0-serving" in blob and "hermes-gpu-schedule-27b" in blob


def test_owner_stop_releases_the_unit(tmp_path):
    db = tmp_path / "state.db"
    _mk_state_db(db, [("s1", "trek0-serving", "user"), ("s2", "grpo-smoke", "user")])
    mod = _load_plugin(tmp_path, db)
    _term(mod, "s1", START)
    res = _term(mod, "s1", STOP)
    assert not (isinstance(res, dict) and res.get("action") == "block"), res
    assert not [l for l in _leases(mod, tmp_path)
                if l.get("resource") == "unit:hermes-gpu-schedule-27b.service"]
    # the queue: next arrival takes the released unit
    res2 = _term(mod, "s2", STOP)
    assert not (isinstance(res2, dict) and res2.get("action") == "block"), res2


def test_bare_pgrep_or_status_does_not_mint(tmp_path):
    db = tmp_path / "state.db"
    _mk_state_db(db, [("s1", "auditor", "user")])
    mod = _load_plugin(tmp_path, db)
    _term(mod, "s1", "systemctl --user status hermes-gpu-schedule-27b.service | head")
    _term(mod, "s1", "journalctl --user -u hermes-gpu-schedule-27b.service --since -5m")
    assert not [l for l in _leases(mod, tmp_path) if str(l.get("resource", "")).startswith("unit:")]

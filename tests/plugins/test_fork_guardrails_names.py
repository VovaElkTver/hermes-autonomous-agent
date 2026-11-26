"""fork-guardrails v0.6.0 «имя вместо хэша» (ToDo «Про́риоцепция» §3, Вл 21.11).

Регрессия на 21.11: аудит/lease называли писателя хэшем сессии — «кто это
сделал» оставалось археологией. След (lease-запись, foreign-block сообщение,
audit-строка block-вердикта) обязан нести человекочитаемое имя: pinned
sessions.title из state.db; без него — sid (деградация, не блокировка).
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


def _load_plugin(tmp_path, state_db):
    spec = importlib.util.spec_from_file_location(
        "fg_names_under_test", REPO / "plugins" / "fork-guardrails" / "__init__.py")
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
    mod._REPO_CACHE.clear()  # realpath(/tmp) may be a git toplevel on some hosts
    return mod


def _audits(tmp_path):
    p = tmp_path / "fg" / "audit.jsonl"
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


def _leases(mod, tmp_path):
    d = tmp_path / "fg" / "leases"
    return [json.loads(f.read_text()) for f in d.glob("*.json")] if d.exists() else []


def _write_file(mod, session_id, path, turn_id="t1"):
    return mod.pre_tool_call(tool_name="write_file",
                             args={"path": str(path), "content": "x"},
                             task_id="k", session_id=session_id, tool_call_id="c",
                             turn_id=turn_id, api_request_id="a", middleware_trace=[])


def test_lease_record_names_the_holder(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    _mk_state_db(db, [("s1", "proprioception-append-rule", "user")])
    mod = _load_plugin(tmp_path, db)
    monkeypatch.delenv("TMPDIR", raising=False)
    monkeypatch.setattr(mod, "_excluded_dirs", lambda s: set())
    _write_file(mod, "s1", tmp_path / "proj" / "a.md")
    leases = _leases(mod, tmp_path)
    assert any(l.get("name") == "proprioception-append-rule" for l in leases), leases


def test_foreign_block_message_names_holder(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    _mk_state_db(db, [("s1", "grpo-smoke", "user"), ("s2", "levels-build", "user")])
    mod = _load_plugin(tmp_path, db)
    monkeypatch.delenv("TMPDIR", raising=False)
    monkeypatch.setattr(mod, "_excluded_dirs", lambda s: set())
    _write_file(mod, "s1", tmp_path / "proj" / "a.md")           # minting
    res = _write_file(mod, "s2", tmp_path / "proj" / "b.md")      # foreign write
    blob = json.dumps(res, default=str)
    assert res and '"block"' in blob, res
    assert "grpo-smoke" in blob
    rec = [r for r in _audits(tmp_path) if r.get("action") == "block"]
    assert rec and rec[0].get("session_name") == "levels-build", rec


def test_nameless_holder_degrades_to_sid(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    _mk_state_db(db, [("s1", None, None)])
    mod = _load_plugin(tmp_path, db)
    monkeypatch.delenv("TMPDIR", raising=False)
    monkeypatch.setattr(mod, "_excluded_dirs", lambda s: set())
    _write_file(mod, "s1", tmp_path / "proj" / "a.md")
    leases = _leases(mod, tmp_path)
    assert any(l.get("name") == "s1" for l in leases), leases

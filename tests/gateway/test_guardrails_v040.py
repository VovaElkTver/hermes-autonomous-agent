"""fork-guardrails v0.4.1: M6 write ledger (pure audit — the allowlist block M2
and child scope note M4 were REMOVED by owner word 23.11). Plus M1 compose_stop_note baskets."""
import importlib.util
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _load_plugin(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "fork_guardrails_v041", REPO / "plugins" / "fork-guardrails" / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    settings = {"enabled": True, "log_only": False, "features": {},
                "audit_log": str(tmp_path / "fork-guardrails" / "audit.jsonl")}

    class _Ctx:
        @staticmethod
        def get_config(key, fallback=None):
            return settings.get(key, fallback)

    mod._ctx = _Ctx()
    return mod


KW = {"session_id": "s1", "turn_id": "t1"}


def _read_jsonl(path):
    if not Path(path).exists():
        return []
    return [json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()]


def test_no_allowlist_surface_remains(tmp_path):
    g = _load_plugin(tmp_path)
    assert "remote_write_gate" not in g._FEATURES
    for gone in ("_allowlist_path", "_load_allowlist", "_host_allowed", "_remote_write_check",
                 "ALLOWLIST_FILENAME", "WRITE_PATH_TOOLS"):
        assert not hasattr(g, gone)


def test_remote_write_never_blocks_but_records(tmp_path, monkeypatch):
    g = _load_plugin(tmp_path)
    monkeypatch.setattr(g, "_ssh_md5", lambda h, t: "d41d8cd98f00b204e9800998ecf8427e")
    cmd = "ssh foreign-host tee /data/ORDER.md < local.md"
    assert g._decide("terminal", {"command": cmd}, KW) is None
    led = _read_jsonl(tmp_path / "fork-guardrails" / "write_ledger.jsonl")
    assert led and led[0]["kind"] == "remote_write" and led[0]["hosts"] == ["foreign-host"]
    assert led[0]["md5_before"] == "d41d8cd98f00b204e9800998ecf8427e"
    g.post_tool_call("terminal", {"command": cmd}, **KW, status="ok")
    lines = _read_jsonl(tmp_path / "fork-guardrails" / "write_ledger.jsonl")
    assert any(r["kind"] == "remote_write_result" and r["status"] == "ok" for r in lines)


def test_reads_untouched_by_ledger(tmp_path):
    g = _load_plugin(tmp_path)
    assert g._decide("terminal", {"command": "ssh host1 grep x /etc/y 2>/dev/null"}, KW) is None
    assert g._decide("terminal", {"command": "rsync -az host1:/srv/x/ ./here"}, KW) is None
    assert g._decide("terminal", {"command": "ssh-keygen -R host1"}, KW) is None
    assert _read_jsonl(tmp_path / "fork-guardrails" / "write_ledger.jsonl") == []


def test_unbalanced_quotes_still_recorded(tmp_path, monkeypatch):
    """Live miss 23.11 23:13: shlex ValueError must not skip detection."""
    g = _load_plugin(tmp_path)
    monkeypatch.setattr(g, "_ssh_md5", lambda h, t: None)
    d = g._decide("execute_code", {"code": 'print("probe: ssh gateprobe-host rm -rf /tmp/x")'}, KW)
    assert d is None or d.get("rule_key") != "fork-guardrails:remote_write"
    led = _read_jsonl(tmp_path / "fork-guardrails" / "write_ledger.jsonl")
    assert led and led[0]["hosts"] == ["gateprobe-host"]


def test_existing_guards_still_work(tmp_path):
    g = _load_plugin(tmp_path)
    d = g._decide("terminal", {"command": "git push --force origin main"}, KW)
    assert d and d["action"] == "block"
    d = g._decide("terminal", {"command": "rm -rf /tmp/x"}, KW)
    assert d and d["action"] == "approve" and d["rule_key"] == "fork-guardrails:rm-recursive"


def test_child_prompt_has_no_scope_block():
    import sys
    sys.path.insert(0, str(REPO))
    from tools.delegate_tool_progress import _build_child_system_prompt
    prompt = _build_child_system_prompt("goal x", "ctx", role="leaf")
    assert "<delegation-scope>" not in prompt


# ── M1 baskets ─────────────────────────────────────────────────────────────

class _DB:
    def __init__(self, db_path):
        self.db_path = db_path

    @contextmanager
    def _read_ctx(self):
        conn = sqlite3.connect(self.db_path)
        try:
            yield conn
        finally:
            conn.close()


def _mkdb(tmp_path, rows):
    db = sqlite3.connect(tmp_path / "state.db")
    db.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,"
               "tool_name TEXT, tool_call_id TEXT, tool_calls TEXT, content TEXT, effect_disposition TEXT)")
    db.executemany("INSERT INTO messages VALUES(NULL,?,?,?,?,?,?,?)", rows)
    db.commit()
    db.close()
    return _DB(tmp_path / "state.db")


def _call(cid, name):
    return json.dumps([{"id": cid, "function": {"name": name}}])


def test_compose_stop_note_baskets(tmp_path):
    from gateway.stop_notice import compose_stop_note
    rows = [
        ("s1", "user", None, None, None, "верни v2", None),
        ("s1", "assistant", None, None, _call("call_1", "terminal"), None, None),
        ("s1", "tool", "terminal", "call_1", None, "deleted stuff\n[Command interrupted]", None),
        ("s1", "assistant", None, None, _call("call_2", "execute_code"), None, None),
        ("s1", "tool", "execute_code", "call_2", None, json.dumps({"status": "success"}), None),
        ("s1", "assistant", None, None, _call("call_3", "read_file"), None, None),
        ("s1", "tool", "read_file", "call_3", None, "text", "none"),
        ("s1", "assistant", None, None, _call("call_4", "write_file"), None, None),
        ("s1", "tool", "skill_manage", "call_5", None, "ok", "unknown"),
    ]
    note = compose_stop_note(_mkdb(tmp_path, rows), "s1")
    assert "stopped" in note.lower()
    assert "execute_code" in note.split("confirmed executed")[1].split("\n")[0]
    unk = note.split("UNKNOWN outcome")[1]
    assert "terminal" in unk and "write_file" in unk and "skill_manage" in unk
    assert "read_file" not in unk


def test_compose_falls_back_when_no_side_effects(tmp_path):
    from gateway.stop_notice import compose_stop_note
    rows = [
        ("s1", "user", None, None, None, "вопрос", None),
        ("s1", "assistant", None, None, _call("call_1", "read_file"), None, None),
        ("s1", "tool", "read_file", "call_1", None, "text", None),
    ]
    assert compose_stop_note(_mkdb(tmp_path, rows), "s1") is None

"""fork-guardrails v0.4.1 M6 write ledger + v0.5.0 mandate gate: remote writes/
exports without a turn grant are gated by owner card (auth_epoch intervention
barrier + op-journal fields). The M2 allowlist block and M4 child scope note were
REMOVED by owner word 23.11 — the v0.5.0 gate is NOT an allowlist: grants are
owner-confirmed, task-scoped, epoch-bound."""
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


def test_remote_write_gated_and_ledger_records(tmp_path, monkeypatch):
    """v0.5.0: a remote write WITHOUT a turn grant is gated (owner card), and the
    ledger still records the op with journal fields (audit stays, block was added)."""
    g = _load_plugin(tmp_path)
    monkeypatch.setattr(g, "_ssh_md5", lambda h, t: "d41d8cd98f00b204e9800998ecf8427e")
    cmd = "ssh foreign-host tee /data/ORDER.md < local.md"
    verdict = g._decide("terminal", {"command": cmd}, KW)
    assert verdict and verdict["action"] == "approve"
    assert verdict["rule_key"] == "fork-guardrails:mandate-gate"
    led = _read_jsonl(tmp_path / "fork-guardrails" / "write_ledger.jsonl")
    assert led and led[0]["kind"] == "remote_write" and led[0]["hosts"] == ["foreign-host"]
    assert led[0]["md5_before"] == "d41d8cd98f00b204e9800998ecf8427e"
    assert led[0]["op_id"] and "write" in led[0]["op_class"] and "turn" in led[0]
    g.post_tool_call("terminal", {"command": cmd}, **KW, status="ok")
    lines = _read_jsonl(tmp_path / "fork-guardrails" / "write_ledger.jsonl")
    assert any(r["kind"] == "remote_write_result" and r["status"] == "ok"
               and r["op_id"] == led[0]["op_id"] for r in lines)


def test_reads_untouched_by_ledger(tmp_path):
    g = _load_plugin(tmp_path)
    # pure remote read: no write verb, no local sink -> no gate, no ledger
    assert g._decide("terminal", {"command": "ssh host1 grep x /etc/y 2>/dev/null"}, KW) is None
    assert g._decide("terminal", {"command": "ssh-keygen -R host1"}, KW) is None
    assert _read_jsonl(tmp_path / "fork-guardrails" / "write_ledger.jsonl") == []


def test_remote_download_is_export_gated(tmp_path):
    """v0.5.0: pulling a remote tree locally (rsync remote->local) is an EXPORT,
    not a read — it gates (owner word 23.11: выгрузку нужно отделять)."""
    g = _load_plugin(tmp_path)
    verdict = g._decide("terminal", {"command": "rsync -az host1:/srv/x/ ./here"}, KW)
    assert verdict and verdict["rule_key"] == "fork-guardrails:mandate-gate"
    assert "export" in verdict["message"]
    led = _read_jsonl(tmp_path / "fork-guardrails" / "write_ledger.jsonl")
    assert led and led[0]["hosts"] == ["host1"] and "export" in led[0]["op_class"]


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


def _unstub_tools():
    """tests/plugins/test_fork_guardrails.py registers a bare ModuleType as 'tools'
    (no __path__) into the process-wide sys.modules; any later real import of a
    tools.* submodule then dies with 'tools is not a package'. Drop the stub —
    the real package supplies everything the stub carried."""
    import sys
    mod = sys.modules.get("tools")
    if mod is not None and not hasattr(mod, "__path__"):
        for key in [k for k in sys.modules if k == "tools" or k.startswith("tools.")]:
            del sys.modules[key]


def test_child_prompt_has_no_scope_block():
    import sys
    sys.path.insert(0, str(REPO))
    _unstub_tools()
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
    _unstub_tools()  # agent.tool_result_classification imports tools.* at module level
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

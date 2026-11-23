"""v0.4.0 fork-guardrails: M2 remote-write gate, M4 child scope note, M6 write ledger;
plus M1 compose_stop_note baskets. Regression cover for the dgx98 incident (23.11)."""
import importlib.util
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]


def _load_plugin(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "fork_guardrails_v040", REPO / "plugins" / "fork-guardrails" / "__init__.py")
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


# ── M2 gate ────────────────────────────────────────────────────────────────

def test_ssh_write_to_foreign_host_blocks(tmp_path):
    g = _load_plugin(tmp_path)
    d = g._decide("terminal", {"command": "ssh dgx98 rm -rf /data/models/x"}, KW)
    assert d and d["action"] == "block"
    assert "dgx98" in d["message"] and d["rule_key"] == "fork-guardrails:remote_write"


def test_remote_read_never_gates(tmp_path):
    g = _load_plugin(tmp_path)
    assert g._decide("terminal", {"command": "ssh dgx98 cat /data/x.log"}, KW) is None
    assert g._decide("terminal", {"command": "scp dgx98:/data/x ./local"}, KW) is None  # download=read
    assert g._decide("terminal", {"command": "ssh-keygen -R dgx98.local"}, KW) is None  # not a transport


def test_allowlisted_host_allows_and_ledgers(tmp_path, monkeypatch):
    g = _load_plugin(tmp_path)
    gdir = tmp_path / "fork-guardrails"
    gdir.mkdir(parents=True, exist_ok=True)
    (gdir / "host-allowlist").write_text("# owner word 23.11\ndgx98\n")
    monkeypatch.setattr(g, "_ssh_md5", lambda h, t: "d41d8cd98f00b204e9800998ecf8427e")
    assert g._decide("terminal", {"command": "ssh dgx98 tee /data/ORDER.md < local.md"}, KW) is None
    led = _read_jsonl(gdir / "write_ledger.jsonl")
    assert led and led[0]["kind"] == "remote_write" and led[0]["decision"] == "allow"
    assert led[0]["md5_before"] == "d41d8cd98f00b204e9800998ecf8427e"  # single exact target
    g.post_tool_call("terminal", {"command": "ssh dgx98 tee /data/ORDER.md < local.md"}, **KW, status="ok")
    assert any(r["kind"] == "remote_write_result" for r in _read_jsonl(gdir / "write_ledger.jsonl"))


def test_log_only_never_blocks_but_records(tmp_path):
    g = _load_plugin(tmp_path)
    real = g._settings

    def patched():
        s = real()
        s["log_only"] = True
        return s
    g._settings = patched
    try:
        assert g._decide("terminal", {"command": "ssh elchost systemctl restart x"}, KW) is None
    finally:
        g._settings = real
    audit = _read_jsonl(tmp_path / "fork-guardrails" / "audit.jsonl")
    assert audit and audit[-1]["kind"] == "would_block"
    led = _read_jsonl(tmp_path / "fork-guardrails" / "write_ledger.jsonl")
    assert led and led[0]["decision"] == "would_block"


def test_allowlist_is_owner_property(tmp_path):
    g = _load_plugin(tmp_path)
    gdir = tmp_path / "fork-guardrails"
    gdir.mkdir(parents=True, exist_ok=True)
    d = g._decide("patch", {"path": str(gdir / "host-allowlist"), "old_string": "a", "new_string": "b"}, KW)
    assert d and d["action"] == "block" and d["rule_key"] == "fork-guardrails:allowlist_write"
    d = g._decide("terminal", {"command": f"echo elchost >> {gdir}/host-allowlist"}, KW)
    assert d and d["action"] == "block" and d["rule_key"] == "fork-guardrails:allowlist_write"
    # reading it is fine
    assert g._decide("terminal", {"command": f"cat {gdir}/host-allowlist"}, KW) is None


def test_host_extraction_forms(tmp_path):
    g = _load_plugin(tmp_path)
    gdir = tmp_path / "fork-guardrails"
    gdir.mkdir(parents=True, exist_ok=True)
    (gdir / "host-allowlist").write_text("192.168.9.98\n")
    # user@ + alias with options: alias NOT in allowlist -> block names the alias
    d = g._decide("terminal", {"command": "ssh -p 22 -i ~/.ssh/id dgx002@dgx98 rm -f /x"}, KW)
    assert d and "dgx98" in d["message"]
    # user@IP: host part matches the allowlisted IP
    assert g._decide("terminal", {"command": "ssh dgx002@192.168.9.98 rm -f /x"}, KW) is None
    # rsync upload (remote dest) gates, download does not
    d = g._decide("terminal", {"command": "rsync -az file.txt root@10.0.0.9:/srv/x/"}, KW)
    assert d and d["action"] == "block"
    assert g._decide("terminal", {"command": "rsync -az root@10.0.0.9:/srv/x/ ./here"}, KW) is None


def test_stderr_redirect_is_not_a_write(tmp_path):
    g = _load_plugin(tmp_path)
    assert g._decide("terminal", {"command": "ssh host1 grep x /etc/y 2>/dev/null"}, KW) is None


def test_unbalanced_quotes_still_gates(tmp_path):
    """Live incident 23.11 23:13: shlex ValueError made the gate skip its own text."""
    g = _load_plugin(tmp_path)
    d = g._decide("execute_code", {"code": 'print("dry-run gate probe: ssh gateprobe-host rm -rf /tmp/x")'}, KW)
    assert d and d["action"] == "block" and d["rule_key"] == "fork-guardrails:remote_write"


# ── M4 child scope note ────────────────────────────────────────────────────

def test_child_prompt_carries_delegation_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    gdir = tmp_path / "fork-guardrails"
    gdir.mkdir(parents=True, exist_ok=True)
    (gdir / "host-allowlist").write_text("dgx98\n# comment\n")
    import sys
    sys.path.insert(0, str(REPO))
    from tools.delegate_tool_progress import _build_child_system_prompt
    prompt = _build_child_system_prompt("goal x", "ctx", role="leaf")
    assert "<delegation-scope>" in prompt and "dgx98" in prompt
    (gdir / "host-allowlist").unlink()
    prompt2 = _build_child_system_prompt("goal x", "ctx", role="leaf")
    assert "NONE (allowlist empty)" in prompt2


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
    assert "read_file" not in unk  # read-only noise stays out


def test_compose_falls_back_when_no_side_effects(tmp_path):
    from gateway.stop_notice import compose_stop_note
    rows = [
        ("s1", "user", None, None, None, "вопрос", None),
        ("s1", "assistant", None, None, _call("call_1", "read_file"), None, None),
        ("s1", "tool", "read_file", "call_1", None, "text", None),
    ]
    assert compose_stop_note(_mkdb(tmp_path, rows), "s1") is None

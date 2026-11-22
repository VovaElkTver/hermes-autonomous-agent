"""Write-permission plugin (plugins/write-permission/) — DoFirst §3.2 D2.

The mechanical half of the owner's write-permission doctrine (SOUL R13, 22.11):
a write permission needs an action verb over the content AND an object, is
one-shot, and a cleanup is a terminal state (no records after the deletion
report until the next verb command). The verb judgment lives in the
write-permission skill; this plugin enforces the mechanical post-delete lock,
audits every verdict (the M.3/M.4 surface), and injects active constraints
into the turn context.

Pinned contract:
- zero settings → enforcing defaults; no registry → no opinion (allow);
- a successful `rm -r/-rf` sets a session-scoped lock on the target;
- write tools touching a locked path are blocked (path-arg tools on the path,
  free-text tools only when the line carries a write hint — the gate fires on
  edits, not on mentions, 17.11 doctrine);
- log_only downgrades every verdict to allow + audit line, zero behavior impact;
- features.* toggles flip individual guards; enabled=false is a full stop;
- pre_llm_call surfaces active locks/constraints; on_session_finalize clears
  locks and keeps operator constraints;
- a corrupted registry fails open (allow), never raises.

Same standalone-load convention as test_fork_guardrails_toggles.py; audit and
registry assertions use explicit tmp_path dirs via the settings surface so the
real home is never touched (home_io_guard would trip on default paths).
"""

import importlib.util
import json
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _load_plugin():
    plugin_dir = ROOT / "plugins" / "write-permission"
    spec = importlib.util.spec_from_file_location(
        "write_permission_under_test", plugin_dir / "__init__.py",
        submodule_search_locations=[str(plugin_dir)])
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "write_permission_under_test"
    mod.__path__ = [str(plugin_dir)]
    sys.modules["write_permission_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


wp = _load_plugin()


class SettingsCtx:
    """PluginContext stand-in: register_hook + hot get_config over a mutable dict."""

    def __init__(self, settings: dict | None = None):
        self.settings = settings if settings is not None else {}
        self.registered: dict = {}

    def register_hook(self, name, cb):
        self.registered.setdefault(name, []).append(cb)

    def get_config(self, key, default=None):
        return self.settings.get(key, default)


class BareCtx:
    def __init__(self):
        self.registered = {}

    def register_hook(self, name, cb):
        self.registered.setdefault(name, []).append(cb)


def _settings(tmp_path, **overrides):
    s = {"enabled": True, "log_only": False,
         "audit_log": str(tmp_path / "audit.jsonl"),
         "registry_dir": str(tmp_path / "reg"),
         "lock_scope": "session",
         "features": {"post_delete_lock": True, "auto_lock_on_rm": True,
                      "inject_constraints": True, "audit_log": True}}
    s.update(overrides)
    return s


@pytest.fixture()
def ctx(tmp_path):
    c = SettingsCtx(_settings(tmp_path))
    wp.register(c)
    return c


def _audit_lines(ctx):
    p = pathlib.Path(ctx.settings["audit_log"])
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line]


def _registry(ctx, sid="s1") -> dict | None:
    p = pathlib.Path(ctx.settings["registry_dir"]) / (sid + ".json")
    if not p.exists():
        return None
    return json.loads(p.read_text())


LOCK = "/tmp/wp-project"  # the deleted target every scenario revolves around


def test_register_wires_all_four_hooks(ctx):
    assert set(ctx.registered) == {"pre_tool_call", "post_tool_call",
                                   "pre_llm_call", "on_session_finalize"}


def test_no_registry_no_session_no_opinion(ctx):
    assert wp.pre_tool_call("write_file", {"path": LOCK + "/a.py"},
                            session_id="s1") is None
    assert wp.post_tool_call("terminal", {"command": f"rm -rf {LOCK}"}) is None
    assert wp.pre_llm_call(session_id="s1") == {}


def test_rm_recursive_sets_session_lock(ctx):
    wp.post_tool_call("terminal", {"command": f"rm -rf {LOCK}"},
                      status="ok", session_id="s1", turn_id="t1")
    reg = _registry(ctx)
    assert reg is not None
    assert len(reg["locks"]) == 1
    lock = reg["locks"][0]
    assert lock["path"] == LOCK
    assert lock["reason"] == "post-delete"
    assert lock["turn_id"] == "t1"
    assert "created" in lock
    assert _audit_lines(ctx)[-1]["kind"] == "lock-set"


def test_rm_without_recursive_flag_sets_no_lock(ctx):
    wp.post_tool_call("terminal", {"command": f"rm {LOCK}/a.py"},
                      status="ok", session_id="s1")
    assert _registry(ctx) is None


def test_failed_rm_sets_no_lock(ctx):
    wp.post_tool_call("terminal", {"command": f"rm -rf {LOCK}"},
                      status="error", session_id="s1")
    assert _registry(ctx) is None


def test_write_tool_touching_lock_is_blocked(ctx):
    wp.post_tool_call("terminal", {"command": f"rm -rf {LOCK}"},
                      status="ok", session_id="s1", turn_id="t1")
    v = wp.pre_tool_call("write_file", {"path": f"{LOCK}/src/app.py"},
                         session_id="s1", turn_id="t2")
    assert v is not None and v["action"] == "block"
    assert v["rule_key"] == "write-permission:post-delete-lock"
    assert _audit_lines(ctx)[-1]["kind"] == "blocked"


def test_lock_covers_descendants_but_not_siblings(ctx):
    wp.post_tool_call("terminal", {"command": f"rm -rf {LOCK}"},
                      status="ok", session_id="s1")
    assert wp.pre_tool_call("patch", {"path": f"{LOCK}/deep/x.md"}, session_id="s1")["action"] == "block"
    assert wp.pre_tool_call("patch", {"path": "/tmp/other/x.md"}, session_id="s1") is None


def test_read_mention_stays_free(ctx):
    """The gate fires on edits, not on mentions: a read line stays allowed."""
    wp.post_tool_call("terminal", {"command": f"rm -rf {LOCK}"},
                      status="ok", session_id="s1")
    assert wp.pre_tool_call("terminal", {"command": f"grep -r foo {LOCK}/src"},
                            session_id="s1") is None


def test_free_text_write_hint_is_blocked(ctx):
    wp.post_tool_call("terminal", {"command": f"rm -rf {LOCK}"},
                      status="ok", session_id="s1")
    v = wp.pre_tool_call("terminal", {"command": f"echo hi > {LOCK}/recreate.txt"},
                         session_id="s1")
    assert v is not None and v["action"] == "block"
    v2 = wp.pre_tool_call("execute_code", {"code": f"open('{LOCK}/b.py','w').close()"},
                          session_id="s1")
    assert v2 is not None and v2["action"] == "block"


def test_lock_only_binds_its_session(ctx):
    wp.post_tool_call("terminal", {"command": f"rm -rf {LOCK}"},
                      status="ok", session_id="s1")
    assert wp.pre_tool_call("write_file", {"path": f"{LOCK}/a.py"},
                            session_id="s2") is None


def test_log_only_allows_but_audits(ctx):
    ctx.settings["log_only"] = True
    wp.post_tool_call("terminal", {"command": f"rm -rf {LOCK}"},
                      status="ok", session_id="s1")
    v = wp.pre_tool_call("write_file", {"path": f"{LOCK}/a.py"}, session_id="s1")
    assert v is None
    kinds = [r["kind"] for r in _audit_lines(ctx)]
    assert "would_block" in kinds and "blocked" not in kinds


def test_feature_toggles(ctx):
    wp.post_tool_call("terminal", {"command": f"rm -rf {LOCK}"},
                      status="ok", session_id="s1")
    ctx.settings["features"] = {"post_delete_lock": False, "auto_lock_on_rm": True,
                                "inject_constraints": True, "audit_log": True}
    assert wp.pre_tool_call("write_file", {"path": f"{LOCK}/a.py"}, session_id="s1") is None
    ctx.settings["features"]["post_delete_lock"] = True
    ctx.settings["enabled"] = False
    assert wp.pre_tool_call("write_file", {"path": f"{LOCK}/a.py"}, session_id="s1") is None


def test_turn_scope_locks_only_their_turn(ctx):
    ctx.settings["lock_scope"] = "turn"
    wp.post_tool_call("terminal", {"command": f"rm -rf {LOCK}"},
                       status="ok", session_id="s1", turn_id="t1")
    assert wp.pre_tool_call("write_file", {"path": f"{LOCK}/a.py"},
                            session_id="s1", turn_id="t1")["action"] == "block"
    assert wp.pre_tool_call("write_file", {"path": f"{LOCK}/a.py"},
                            session_id="s1", turn_id="t2") is None


def test_pre_llm_call_injects_active_constraints(ctx):
    wp.post_tool_call("terminal", {"command": f"rm -rf {LOCK}"},
                      status="ok", session_id="s1")
    reg = _registry(ctx, "s1")
    reg["constraints"] = ["чистка завершена: записей до новой команды с глаголом нет"]
    reg_path = pathlib.Path(ctx.settings["registry_dir"]) / "s1.json"
    reg_path.write_text(json.dumps(reg, ensure_ascii=False), encoding="utf-8")
    out = wp.pre_llm_call(session_id="s1", turn_id="t9")
    assert "post-delete lock" in out["context"] and LOCK in out["context"]
    assert "чистка завершена" in out["context"]


def test_no_locks_no_injection(ctx):
    assert wp.pre_llm_call(session_id="s1") == {}


def test_finalize_clears_locks_keeps_constraints(ctx):
    wp.post_tool_call("terminal", {"command": f"rm -rf {LOCK}"},
                      status="ok", session_id="s1")
    reg = _registry(ctx, "s1")
    reg["constraints"] = ["owner constraint"]
    reg_path = pathlib.Path(ctx.settings["registry_dir"]) / "s1.json"
    reg_path.write_text(json.dumps(reg), encoding="utf-8")
    wp.on_session_finalize(session_id="s1")
    after = _registry(ctx, "s1")
    assert after["locks"] == []
    assert after["constraints"] == ["owner constraint"]


def test_corrupted_registry_fails_open(ctx):
    reg_path = pathlib.Path(ctx.settings["registry_dir"]) / "s1.json"
    reg_path.parent.mkdir(parents=True, exist_ok=True)
    reg_path.write_text("{not json", encoding="utf-8")
    assert wp.pre_tool_call("write_file", {"path": f"{LOCK}/a.py"}, session_id="s1") is None
    assert wp.pre_llm_call(session_id="s1") == {}


def test_bare_ctx_degrades_to_enforcing_defaults(tmp_path, monkeypatch):
    """A ctx without get_config → enforcing defaults; no registry → no opinion.

    Default registry/audit paths live under $HOME, so point HOME at tmp_path:
    the degraded read lands in the hermetic tree and finds no locks.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    bare = BareCtx()
    wp.register(bare)
    assert wp.pre_tool_call("write_file", {"path": LOCK + "/a.py"}, session_id="s1") is None
    assert wp.pre_llm_call(session_id="s1") == {}
    assert wp.post_tool_call("terminal", {"command": f"rm -rf {LOCK}"},
                             status="ok", session_id="s1") is None
    # default registry dir under the fake HOME now holds the auto lock
    reg_file = tmp_path / ".hermes" / "state" / "write-permission" / "s1.json"
    assert reg_file.exists()

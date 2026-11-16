"""B1+B3 tests (TZ-2026-11-16-dsh-B): the tool-call contract behind
``tools.call_contract_v2`` (default OFF).

Covers the frozen call identity, the monotonic final guard pass, identical-call
repeat detection, the three lowering-guard adapters (floors, pre-exec, write
gate), the ``event_observations`` call-identity row (two-step migration, stage
2 writer), and the dispatch-level wiring — including the guarantee that the
flag-OFF path stays byte-identical to the legacy dispatch.
"""
import dataclasses
import json

import pytest

from tools import tool_contract
from tools.registry import ToolRegistry
from tools.tool_contract import (
    Allow,
    ArgsMutatedError,
    Deny,
    apply_guards,
    build_call_spec,
    canonical_args_hash,
    clear_repeat_index,
    clear_tool_guards,
    denial_result,
    is_repeat,
    note_repeat,
    register_tool_guard,
)


@pytest.fixture(autouse=True)
def _clean_contract_state():
    """Isolate the process-global contract state (guards, repeat index, the
    one-shot guard-registration flag) around every test."""
    clear_tool_guards()
    clear_repeat_index()
    tool_contract._ensured = False
    yield
    clear_tool_guards()
    clear_repeat_index()
    tool_contract._ensured = False


def _flag_on(monkeypatch, debug=False):
    """Point the live config read at the flag-ON values (pattern: the
    module-private config accessor, as in tests/tools/test_approval_ledger)."""
    monkeypatch.setattr(
        tool_contract, "_tools_config",
        lambda: {"call_contract_v2": True, "call_spec_debug": debug})


def _make_registry(handler=None, calls=None):
    reg = ToolRegistry()

    def h(args, **_kwargs):
        if calls is not None:
            calls.append(args)
        if handler is not None:
            return handler(args, **_kwargs)
        return json.dumps({"ok": True})

    reg.register(name="probe", toolset="core",
                 schema={"name": "probe", "description": "t",
                         "parameters": {"type": "object"}},
                 handler=h)
    return reg


def _spec(tool="probe", args=None, session="s1"):
    return build_call_spec(tool=tool, args=args if args is not None else {"a": 1},
                           session_key=session)


# ---------------------------------------------------------------------------
# B1(a) + B3: the frozen identity
# ---------------------------------------------------------------------------

def test_spec_is_frozen_and_canonical():
    spec = build_call_spec(tool="terminal", args={"command": "ls", "n": 1},
                           session_key="s1")
    assert spec.call_id and len(spec.call_id) == 32
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.call_id = "tampered"
    # canonical: insertion order does not change the hash; content does.
    assert canonical_args_hash({"a": 1, "b": 2}) == canonical_args_hash({"b": 2, "a": 1})
    assert canonical_args_hash({"a": 1}) != canonical_args_hash({"a": 2})
    assert spec.args_hash == canonical_args_hash({"command": "ls", "n": 1})


def test_spec_call_id_is_unique_per_dispatch():
    a = build_call_spec(tool="probe", args={}, session_key="s1")
    b = build_call_spec(tool="probe", args={}, session_key="s1")
    assert a.args_hash == b.args_hash
    assert a.call_id != b.call_id


# ---------------------------------------------------------------------------
# dispatch wiring: flag OFF is the legacy byte-identical path
# ---------------------------------------------------------------------------

def test_flag_off_dispatch_has_no_contract_meta():
    calls = []
    reg = _make_registry(calls=calls)
    result = reg.dispatch("probe", {"a": 1})
    payload = json.loads(result)
    assert payload["ok"] is True
    assert "meta" not in payload  # no contract block at all
    assert calls == [{"a": 1}]


def test_flag_on_dispatch_carries_call_identity(monkeypatch):
    _flag_on(monkeypatch)
    calls = []
    reg = _make_registry(calls=calls)
    result = reg.dispatch("probe", {"a": 1}, session_id="sess-42")
    payload = json.loads(result)
    assert payload["ok"] is True
    cc = payload["meta"]["call_contract"]
    assert cc["decision"] == "allow"
    assert cc["args_hash"] == canonical_args_hash({"a": 1})
    assert len(cc["call_id"]) == 32
    assert cc["repeat"] is False
    assert cc["args_mutated"] is False
    assert "lowered_by" in cc
    assert calls == [{"a": 1}]


# ---------------------------------------------------------------------------
# B1(b): the monotonic final guard pass
# ---------------------------------------------------------------------------

def test_guard_lowering_is_structural_and_absorbing():
    spec = _spec()
    lowered = []

    def g1(spec, decision, args):
        from tools.tool_contract import Denial
        lowered.append("g1")
        return Denial(reason="first layer denies", layer="g1")

    def g2(spec, decision, args):
        from tools.tool_contract import Denial
        lowered.append("g2")
        return Denial(reason="second layer, must not rewrite", layer="g2")

    register_tool_guard("g1", g1)
    register_tool_guard("g2", g2)
    result = apply_guards(spec, Allow(), {"a": 1})
    assert isinstance(result, Deny)
    assert result.layer == "g1"  # the FIRST lowering layer is remembered
    assert result.reason == "first layer denies"
    assert lowered == ["g1", "g2"]  # later guards still run, but cannot rewrite

    # a guard that answers nothing preserves the decision (allow AND ask both pass through)
    from tools.tool_contract import Ask
    clear_tool_guards()
    allow = Allow()
    ask = Ask(reason="owed")
    assert apply_guards(spec, allow, {"a": 1}) is allow
    assert apply_guards(spec, ask, {"a": 1}) is ask


def test_raising_guard_fails_closed():
    spec = _spec()

    def boom(spec, decision, args):
        raise RuntimeError("guard exploded")

    register_tool_guard("g1", boom)
    result = apply_guards(spec, Allow(), {"a": 1})
    assert isinstance(result, Deny)
    assert result.layer == "g1"
    assert "failed closed" in result.reason


def test_guard_cannot_undo_a_lowering():
    """Deny is absorbing: once any guard lowered to Deny, later guard answers
    are no-ops — no listener can re-allow (B1: nothing downstream of the pass
    may re-allow)."""
    from tools.tool_contract import Denial

    spec = _spec()
    register_tool_guard("hard", lambda s, d, a: Denial(reason="hard floor", layer="hard"))

    class ReAllowGuard:
        """A malformed 'guard' that would try to return an allow-ish answer —
        the type system does not allow it (Guard -> None | Denial); simulating
        the intent by returning a Denial for another layer: it must not stick."""
        @staticmethod
        def g(spec, decision, args):
            return Denial(reason="rewritten", layer="other")

    clear_tool_guards()
    register_tool_guard("hard", lambda s, d, a: Denial(reason="hard floor", layer="hard"))
    register_tool_guard("other", ReAllowGuard.g)
    result = apply_guards(spec, Allow(), {})
    assert isinstance(result, Deny)
    assert result.layer == "hard"
    assert result.reason == "hard floor"


# ---------------------------------------------------------------------------
# B1(v): identical-call repeat detection
# ---------------------------------------------------------------------------

def test_repeat_detection_within_window():
    spec = _spec()
    assert note_repeat(spec) is False          # first call
    spec2 = build_call_spec(tool="probe", args={"a": 1}, session_key="s1")
    assert spec2.args_hash == spec.args_hash
    assert note_repeat(spec2) is True           # identical call in the window
    assert is_repeat(spec2) is True             # pure query, no recording
    other = build_call_spec(tool="probe", args={"a": 2}, session_key="s1")
    assert note_repeat(other) is False          # different args: no repeat
    other_session = build_call_spec(tool="probe", args={"a": 1}, session_key="s2")
    assert note_repeat(other_session) is False  # different session: no repeat
    clear_repeat_index()
    assert note_repeat(spec) is False           # after the reset, first again


# ---------------------------------------------------------------------------
# B1(c): the floor adapter — the floors as ONE structured denial
# ---------------------------------------------------------------------------

def test_floor_denial_adapter_maps_floor_verdicts(monkeypatch):
    from tools import approval
    from tools.approval_floors import floor_denial

    cases = [
        ({"message": "BLOCKED (hardline) command: rm -rf /"}, "hardline"),
        ({"user_deny": True, "message": "denied by your approvals.deny"}, "user_deny"),
        (None, None),
        ({"message": "BLOCKED (sudo-stdin)"}, "sudo_stdin"),
    ]
    for verdict, expected_floor in cases:
        monkeypatch.setattr(approval, "_floor_block", lambda cmd, sudo_guard=False: verdict)
        denial = floor_denial("some command")
        if expected_floor is None:
            assert denial is None
        else:
            assert isinstance(denial, tool_contract.Denial)
            assert denial.layer == "approval_floors"
            assert denial.info["floor"] == expected_floor


def test_command_floor_guard_only_touches_command_tools(monkeypatch):
    from tools import approval
    _flag_on(monkeypatch)
    monkeypatch.setattr(approval, "_floor_block",
                       lambda cmd, sudo_guard=False:
                       {"message": "BLOCKED (hardline) command"} if "rm -rf" in cmd else None)
    tool_contract.ensure_contract_guards()
    spec_floor = _spec(tool="terminal", args={"command": "rm -rf /"})
    assert isinstance(apply_guards(spec_floor, Allow(), {"command": "rm -rf /"}), Deny)
    spec_safe = _spec(tool="terminal", args={"command": "ls"})
    allow = Allow()
    assert apply_guards(spec_safe, allow, {"command": "ls"}) is allow
    spec_noncmd = _spec(tool="probe", args={"command": "rm -rf /"})
    allow2 = Allow()
    assert apply_guards(spec_noncmd, allow2, {"command": "rm -rf /"}) is allow2


# ---------------------------------------------------------------------------
# B1(c): the pre-exec guard adapter (terminal workdir / self-repo / lock)
# ---------------------------------------------------------------------------

def test_preexec_guard_reports_bad_workdir(monkeypatch):
    import tools.terminal_tool_guards as ttg
    monkeypatch.setattr(ttg, "_validate_workdir", lambda w: "workdir does not exist")
    spec = _spec(tool="terminal", args={"command": "ls", "workdir": "/nope"})
    from tools.tool_contract import _terminal_preexec_guard
    denial = _terminal_preexec_guard(spec, Allow(), {"command": "ls", "workdir": "/nope"})
    assert denial is not None
    assert denial.layer == "pre_exec_guards"
    assert denial.info["pre_exec"] == "workdir"


def test_preexec_guard_defers_for_non_terminal_tools():
    from tools.tool_contract import _terminal_preexec_guard
    spec = _spec(tool="probe", args={"command": "x"})
    assert _terminal_preexec_guard(spec, Allow(), {"command": "x"}) is None


def test_preexec_guard_blocks_locked_skill_writes(monkeypatch):
    import tools.terminal_tool_guards as ttg
    monkeypatch.setattr(ttg, "locked_skill_block",
                        lambda **kw: json.dumps({"error": "skill locked"}))
    from tools.tool_contract import _terminal_preexec_guard
    spec = _spec(tool="terminal", args={"command": "echo 1 > skill/notes.md"})
    denial = _terminal_preexec_guard(spec, Allow(), {"command": "echo 1"})
    assert denial is not None
    assert denial.info["pre_exec"] == "locked_skill"


# ---------------------------------------------------------------------------
# B1(c): the write-gate adapter (protected agent-instruction writes)
# ---------------------------------------------------------------------------

def test_write_gate_denies_protected_write_without_human(monkeypatch):
    from tools import approval
    import tools.file_tools_write_guards as ftw
    monkeypatch.setattr(ftw, "_protected_instruction_reason",
                        lambda path, **kw: "protected agent instruction file")
    monkeypatch.setattr(approval, "_presence", lambda cb=None: (None, False, False, False))
    from tools.tool_contract import _write_gate_guard
    spec = _spec(tool="write_file", args={"path": "/home/u/.hermes/SKILL.md"})
    denial = _write_gate_guard(spec, Allow(), {"path": "/home/u/.hermes/SKILL.md"})
    assert denial is not None
    assert denial.layer == "write_gate"
    assert denial.info["write_gate"] == "protected_instruction"


def test_write_gate_defers_when_a_human_channel_exists(monkeypatch):
    from tools import approval
    import tools.file_tools_write_guards as ftw
    monkeypatch.setattr(ftw, "_protected_instruction_reason",
                        lambda path, **kw: "protected agent instruction file")
    # a live CLI surface: the handler's interactive gate stays the decider
    monkeypatch.setattr(approval, "_presence", lambda cb=None: (None, True, False, False))
    from tools.tool_contract import _write_gate_guard
    spec = _spec(tool="patch", args={"path": "/home/u/.hermes/SKILL.md"})
    assert _write_gate_guard(spec, Allow(), {"path": "/home/u/.hermes/SKILL.md"}) is None


def test_write_gate_ignores_unprotected_paths_and_other_tools(monkeypatch):
    import tools.file_tools_write_guards as ftw
    monkeypatch.setattr(ftw, "_protected_instruction_reason", lambda path, **kw: None)
    from tools.tool_contract import _write_gate_guard
    spec = _spec(tool="write_file", args={"path": "/tmp/plain.txt"})
    assert _write_gate_guard(spec, Allow(), {"path": "/tmp/plain.txt"}) is None
    spec_other = _spec(tool="terminal", args={"path": "/tmp/plain.txt"})
    assert _write_gate_guard(spec_other, Allow(), {"path": "/tmp/plain.txt"}) is None


# ---------------------------------------------------------------------------
# B3: identity into the result meta and the event_observations row
# ---------------------------------------------------------------------------

def test_denial_result_carries_the_contract_meta():
    spec = _spec(tool="terminal", args={"command": "rm -rf /"})
    deny = Deny(reason="floors", info={"floor": "hardline"}, layer="approval_floors")
    payload = json.loads(denial_result("terminal", spec, deny, repeat=True))
    assert "denied" in payload["error"]
    cc = payload["meta"]["call_contract"]
    assert cc["decision"] == "deny"
    assert cc["lowered_by"] == "approval_floors"
    assert cc["info"] == {"floor": "hardline"}
    assert cc["repeat"] is True
    assert payload["meta"]["call_contract"]["args_hash"] == canonical_args_hash({"command": "rm -rf /"})


def test_args_mutation_inside_wrapper_is_flagged(monkeypatch):
    _flag_on(monkeypatch)
    seen = []

    def mutating(args, **_kw):
        seen.append(args["a"])
        args["a"] = 99  # the wrapper rewrites the arguments in place
        return json.dumps({"ok": True})

    reg = _make_registry(handler=mutating)
    result = reg.dispatch("probe", {"a": 1})
    payload = json.loads(result)
    assert payload["meta"]["call_contract"]["args_mutated"] is True
    assert seen == [1]  # the handler saw the dispatch-time value, then rewrote it


def test_args_mutation_raises_under_debug_flag(monkeypatch):
    _flag_on(monkeypatch, debug=True)

    def mutating(args, **_kw):
        args["a"] = 99
        return json.dumps({"ok": True})

    reg = _make_registry(handler=mutating)
    with pytest.raises(ArgsMutatedError):
        reg.dispatch("probe", {"a": 1})


def test_dispatch_error_path_still_carries_the_contract_meta(monkeypatch):
    _flag_on(monkeypatch)

    def exploding(args, **_kw):
        raise ValueError("kaboom")

    reg = _make_registry(handler=exploding)
    result = reg.dispatch("probe", {"a": 1})
    payload = json.loads(result)
    assert "kaboom" in payload["error"]
    cc = payload["meta"]["call_contract"]
    assert cc["decision"] == "allow"  # the guard's verdict stands; the handler failed
    assert cc["args_mutated"] is False


def test_dispatch_guard_denial_short_circuits_the_handler(monkeypatch):
    _flag_on(monkeypatch)
    calls = []
    reg = _make_registry(calls=calls)

    from tools.tool_contract import Denial
    register_tool_guard("test_floor",
                        lambda s, d, a: Denial(reason="test floor", layer="test_floor"))
    result = reg.dispatch("probe", {"a": 1})
    assert calls == []  # the handler never ran
    payload = json.loads(result)
    assert "denied" in payload["error"]
    assert payload["meta"]["call_contract"]["decision"] == "deny"
    assert payload["meta"]["call_contract"]["lowered_by"] == "test_floor"


# ---------------------------------------------------------------------------
# B3: the event_observations row (two-step migration, stage-2 writer)
# ---------------------------------------------------------------------------

def test_record_tool_call_observation_writes_the_call_identity_row(tmp_path):
    from controller import model as cm
    from controller.transport import record_tool_call_observation
    conn = cm.connect(tmp_path / "state.db")
    spec = _spec(tool="terminal", args={"command": "ls"})
    deny = Deny(reason="floors", layer="approval_floors")
    out = record_tool_call_observation(conn, spec=spec, decision=deny, repeat=True)
    row = conn.execute(
        "SELECT call_id, args_hash, decision, lowered_by, event_type,"
        " identity_consistency, server_retrievability FROM event_observations"
        " WHERE call_id = ?", (spec.call_id,)).fetchone()
    assert row is not None
    assert row["args_hash"] == spec.args_hash
    assert row["decision"] == "deny"
    assert row["lowered_by"] == "approval_floors"
    assert row["event_type"] == "tool_call"
    # the six transport characteristics: settled by construction for a tool event
    assert row["identity_consistency"] == "ok"
    assert row["server_retrievability"] == "n/a"
    assert out["call_id"] == spec.call_id
    # no transport_observations row: the reconciler's JOIN never picks this up
    assert conn.execute(
        "SELECT COUNT(*) FROM transport_observations").fetchone()[0] == 0
    conn.close()


def test_record_call_observation_is_a_noop_when_flag_off(monkeypatch):
    import controller.model as cm
    seen = []

    def _boom():
        seen.append(1)
        raise AssertionError("_connect must not be called with the flag off")

    monkeypatch.setattr(cm, "_connect", _boom)
    spec = _spec()
    tool_contract.record_call_observation(spec, Allow())  # must not raise, must not connect
    assert seen == []


# ---------------------------------------------------------------------------
# B3: two-step migration, stage 1 — the legacy store gains the columns
# ---------------------------------------------------------------------------

_LEGACY_EVENT_OBSERVATIONS_DDL = """
CREATE TABLE IF NOT EXISTS event_observations (
  event_obs_id TEXT PRIMARY KEY,
  observation_id TEXT NOT NULL,
  room_id TEXT NOT NULL,
  event_id TEXT NOT NULL,
  event_type TEXT NOT NULL DEFAULT '',
  sender TEXT NOT NULL DEFAULT '',
  content_hash TEXT NOT NULL DEFAULT '',
  relation TEXT NOT NULL DEFAULT '',
  normalized_ref TEXT NOT NULL DEFAULT '',
  identity_consistency TEXT NOT NULL DEFAULT 'pending',
  server_retrievability TEXT NOT NULL DEFAULT 'pending',
  decryption_status TEXT NOT NULL DEFAULT 'pending',
  redaction_status TEXT NOT NULL DEFAULT 'pending',
  timeline_placement TEXT NOT NULL DEFAULT 'pending',
  normalization_consistency TEXT NOT NULL DEFAULT 'pending',
  created_at REAL NOT NULL
);
"""


def test_reconcile_migrates_pre_b3_event_observations(tmp_path):
    """Stage 1 of the two-step migration: a pre-B3 store (event_observations without
    the call-identity columns) gains the four nullable columns on the next
    reconcile_state_schema — additively, with the existing rows untouched.
    (The deferred index on call_id lands with the SessionDB init path, same
    house rule as idx_sessions_local_created_started.)"""
    import sqlite3
    from hermes_state_schema import reconcile_state_schema
    db = tmp_path / "state.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(_LEGACY_EVENT_OBSERVATIONS_DDL)
    conn.execute(
        "INSERT INTO event_observations"
        " (event_obs_id, observation_id, room_id, event_id, event_type, created_at)"
        " VALUES ('eo-1', 'obs-1', '!room:mx', '$evt', 'm.room.message', 123.0)")
    conn.commit()
    reconcile_state_schema(conn)
    cols = {row[1] for row in conn.execute(
        "PRAGMA table_info(event_observations)")}
    for new_col in ("call_id", "args_hash", "decision", "lowered_by"):
        assert new_col in cols
    # the legacy row survives the additive migration, its new columns NULL
    row = conn.execute(
        "SELECT call_id, args_hash, decision, lowered_by FROM event_observations"
        " WHERE event_obs_id = 'eo-1'").fetchone()
    assert row == (None, None, None, None)
    conn.close()


def test_record_call_observation_writes_when_flag_on(monkeypatch, tmp_path):
    _flag_on(monkeypatch)
    import controller.model as cm
    conn = cm.connect(tmp_path / "state.db")
    conn.close()  # the recorder opens its own connection and closes it
    monkeypatch.setattr(cm, "_connect", lambda: cm.connect(tmp_path / "state.db"))
    spec = _spec(tool="terminal", args={"command": "ls"})
    tool_contract.record_call_observation(spec, Allow(), repeat=False)
    check = cm.connect(tmp_path / "state.db")
    row = check.execute("SELECT call_id, decision FROM event_observations"
                        " WHERE call_id = ?", (spec.call_id,)).fetchone()
    assert row is not None
    assert row["decision"] == "allow"
    check.close()

"""fork-guardrails v0.5.0: intervention barrier (auth_epoch) + remote-write
mandate (mandate_gate) + op-journal fields — regression replay of the disputed
dgx98-incident steps at the hook level (offline: the plugin module is loaded
standalone and the hooks are driven with the dispatcher's kwarg shapes, same
as test_fork_guardrails.py / test_guardrails_v040.py).

Incident chain each test replays:
- the branch wrote to a foreign machine WITHOUT a mandate -> the gate must
  have required the owner card BEFORE the first remote mutation;
- «не успел ничего сделать?» (an owner QUESTION mid-rollback) -> a new owner
  message voids the previous turn's grants, so the rollback could not have
  continued on stale authority;
- «возвращай» misread as scp-out + rm -> export and delete are their own
  op-classes, none covered by a write grant;
- restoring «v2» by NAME (4141b09b vs 4049353d) -> the op journal carries
  op_id/turn/op_class + before/after md5 so a restore addresses an artifact,
  not a remembered label.
"""
import importlib.util
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

WRITE = "ssh dgx98 'cp /home/dgx002/.hermes/scripts/gpu.sh.bak /home/dgx002/.hermes/scripts/gpu.sh'"
EXPORT = "scp dgx98:/home/dgx002/.hermes/scripts/gpu.sh.v2-proposal /home/qwen/quarantine/"
DELETE = "ssh dgx98 'rm /home/dgx002/.hermes/scripts/gpu.sh.bak'"


def _load_plugin(tmp_path, **settings_overrides):
    spec = importlib.util.spec_from_file_location(
        "fg_v05_under_test", REPO / "plugins" / "fork-guardrails" / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    settings = {"enabled": True, "log_only": False, "features": {},
                "audit_log": str(tmp_path / "fg-v05" / "audit.jsonl")}
    settings.update(settings_overrides)

    class _Ctx:
        @staticmethod
        def get_config(key, fallback=None):
            return settings.get(key, fallback)

    mod._ctx = _Ctx()
    return mod


def directive(mod, tool, args, session_id="s1", turn_id="t1"):
    """Mirror _get_pre_tool_call_directive_details precedence.
    Returns (rule_key_or_None, verdict_dict_or_None, raw_pre_tool_result)."""
    res = mod.pre_tool_call(tool_name=tool, args=args, task_id="k",
                             session_id=session_id, tool_call_id="c", turn_id=turn_id,
                             api_request_id="a", middleware_trace=[])
    if not isinstance(res, dict):
        return None, None, res
    if res.get("action") == "block" and res.get("message"):
        return "block", res, res
    if res.get("action") == "approve":
        return res.get("rule_key"), res, res
    return None, None, res


def _read_jsonl(path):
    if not Path(path).exists():
        return []
    return [json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()]


def test_first_remote_mutation_needs_owner_card(tmp_path):
    """The branch's gpu.sh deploy had no mandate — with v0.5 the FIRST remote
    write of a session/turn stops at the owner card, not at the remote shell."""
    g = _load_plugin(tmp_path)
    rule, verdict, _ = directive(g, "terminal", {"command": WRITE})
    assert rule == "fork-guardrails:mandate-gate"
    assert "target state" in verdict["message"] and "basis" in verdict["message"]


def test_approved_write_covers_rest_of_turn_only(tmp_path):
    """Owner approved the write: the same host+class keeps flowing THIS turn
    (no card per step of a pre-agreed plan)..."""
    g = _load_plugin(tmp_path)
    rule, _, _ = directive(g, "terminal", {"command": WRITE})
    assert rule == "fork-guardrails:mandate-gate"
    g.post_tool_call("terminal", {"command": WRITE}, task_id="k", session_id="s1",
                     turn_id="t1", status="ok")
    rule, _, _ = directive(g, "terminal", {"command": WRITE})
    assert rule is None
    # ...but a different op-class (export) still needs its own card
    rule, verdict, _ = directive(g, "terminal", {"command": EXPORT})
    assert rule == "fork-guardrails:mandate-gate"
    assert "export" in verdict["message"]


def test_owner_question_voids_previous_turns_grants(tmp_path):
    """The incident's «не успел ничего сделать?» = a new owner message mid-rollback:
    the barrier must have voided the rollback's grants, so it could not continue."""
    g = _load_plugin(tmp_path)
    directive(g, "terminal", {"command": WRITE})
    g.post_tool_call("terminal", {"command": WRITE}, task_id="k", session_id="s1",
                     turn_id="t1", status="ok")
    g.on_session_end(session_id="s1", turn_id="t1", completed=True, failed=False, interrupted=False)
    out = g.pre_llm_call(session_id="s1", user_message="не успел ничего сделать ?",
                         conversation_history=[], is_first_turn=False, model="m", platform="matrix")
    assert isinstance(out, dict)
    # the same write must stop at the card again — stale authority is dead
    rule, _, _ = directive(g, "terminal", {"command": WRITE}, turn_id="t2")
    assert rule == "fork-guardrails:mandate-gate"


def test_new_message_bumps_persisted_turn(tmp_path):
    g = _load_plugin(tmp_path)
    g.pre_llm_call(session_id="s1", user_message="первое", conversation_history=[],
                   is_first_turn=True, model="m", platform="matrix")
    state = json.loads((tmp_path / "fg-v05" / "auth_state.json").read_text())
    assert state["turn"] == 1
    # a repeated delivery of the SAME message must not re-bump
    g.pre_llm_call(session_id="s1", user_message="первое", conversation_history=[],
                   is_first_turn=False, model="m", platform="matrix")
    state = json.loads((tmp_path / "fg-v05" / "auth_state.json").read_text())
    assert state["turn"] == 1
    g.pre_llm_call(session_id="s1", user_message="второе", conversation_history=[],
                   is_first_turn=False, model="m", platform="matrix")
    state = json.loads((tmp_path / "fg-v05" / "auth_state.json").read_text())
    assert state["turn"] == 2


def test_child_session_inherits_nothing(tmp_path):
    """A branch (fork session) inherits the PARENT's context but NOT its grants:
    only an explicitly owner-confirmed card in the child's own session passes."""
    g = _load_plugin(tmp_path)
    rule, _, _ = directive(g, "terminal", {"command": WRITE}, session_id="parent")
    assert rule == "fork-guardrails:mandate-gate"
    g.post_tool_call("terminal", {"command": WRITE}, task_id="k", session_id="parent",
                     turn_id="t1", status="ok")
    rule, _, _ = directive(g, "terminal", {"command": WRITE}, session_id="parent")
    assert rule is None  # parent's approved class flows for the rest of the turn
    rule, _, _ = directive(g, "terminal", {"command": WRITE}, session_id="child")
    assert rule == "fork-guardrails:mandate-gate"  # the child got nothing for free


def test_delete_and_export_are_own_classes(tmp_path):
    g = _load_plugin(tmp_path)
    rule, _, _ = directive(g, "terminal", {"command": DELETE})
    assert rule == "fork-guardrails:mandate-gate"
    led = _read_jsonl(tmp_path / "fg-v05" / "write_ledger.jsonl")
    assert led and set(led[0]["op_class"]) == {"write", "delete"}
    n_before = len(led)
    rule, _, _ = directive(g, "terminal", {"command": EXPORT})
    assert rule == "fork-guardrails:mandate-gate"
    led = _read_jsonl(tmp_path / "fg-v05" / "write_ledger.jsonl")
    assert led[n_before]["op_class"] == ["export"] and led[n_before]["hosts"] == ["dgx98"]


def test_local_writes_and_remote_reads_untouched(tmp_path):
    g = _load_plugin(tmp_path)
    # local edits stay OUT of the mandate gate (no foreign host involved)
    assert directive(g, "terminal", {"command": "cp a b"})[0] is None
    assert directive(g, "terminal", {"command": "ssh dgx98 'ls /home/dgx002/'"})[0] is None
    assert directive(g, "terminal", {"command": "touch /tmp/local-build"})[0] is None
    # a LOCAL rm -rf is still the pre-existing outbound-approve gate, not the mandate
    rule, _, _ = directive(g, "terminal", {"command": "rm -rf /tmp/local-build"})
    assert rule == "fork-guardrails:rm-recursive"


def test_op_journal_carries_version_chain(tmp_path, monkeypatch):
    """«v2» by label restored the wrong revision — the journal must let a
    restore address op_id + before/after hashes, not a remembered name."""
    g = _load_plugin(tmp_path)
    monkeypatch.setattr(g, "_ssh_md5", lambda h, t: "beforehash0000")
    import threading as _th

    class _InlineThread(_th.Thread):
        def start(self):
            self._target()

    monkeypatch.setattr(_th, "Thread", _InlineThread)  # g.threading IS this module
    single = "ssh dgx98 'touch /home/dgx002/fgprobe'"  # single exact target: md5 chain
    g.pre_tool_call(tool_name="terminal", args={"command": single}, task_id="k",
                    session_id="s1", tool_call_id="c", turn_id="t1",
                    api_request_id="a", middleware_trace=[])
    g.post_tool_call("terminal", {"command": single}, task_id="k", session_id="s1",
                     turn_id="t1", status="ok")
    led = _read_jsonl(tmp_path / "fg-v05" / "write_ledger.jsonl")
    pre = next(r for r in led if r["kind"] == "remote_write")
    assert pre["md5_before"] == "beforehash0000" and pre["targets"]
    assert pre["op_id"] and pre["turn"] is not None and "write" in pre["op_class"]
    after = [r for r in led if r["kind"] == "remote_md5_after"]
    assert after and after[0]["md5"] == "beforehash0000"
    assert after[0]["op_id"] == pre["op_id"]
    res = next(r for r in led if r["kind"] == "remote_write_result")
    assert res["op_id"] == pre["op_id"] and res["status"] == "ok"


def test_log_only_never_enforces(tmp_path):
    g = _load_plugin(tmp_path, log_only=True)
    rule, _verdict, _ = directive(g, "terminal", {"command": WRITE})
    assert rule is None  # no verdict in log-only — dry run, zero impact
    audit = _read_jsonl(tmp_path / "fg-v05" / "audit.jsonl")
    assert any(a["kind"] == "would_approve" and a.get("rule_key") == "fork-guardrails:mandate-gate"
               for a in audit)


def test_restart_keeps_turn_counter_and_no_grant_resurrection(tmp_path):
    g1 = _load_plugin(tmp_path)
    g1.pre_llm_call(session_id="s1", user_message="m1", conversation_history=[],
                    is_first_turn=True, model="m", platform="matrix")
    g1.pre_tool_call(tool_name="terminal", args={"command": WRITE}, task_id="k",
                     session_id="s1", tool_call_id="c", turn_id="t1",
                     api_request_id="a", middleware_trace=[])
    g1.post_tool_call("terminal", {"command": WRITE}, task_id="k", session_id="s1",
                      turn_id="t1", status="ok")
    g2 = _load_plugin(tmp_path)  # a freshly booted process: in-memory grants are gone
    assert not g2._turn_grants.get("s1")
    assert g2._load_turn_state(g2._settings())["turn"] >= 1
    rule, _, _ = directive(g2, "terminal", {"command": WRITE})
    assert rule == "fork-guardrails:mandate-gate"

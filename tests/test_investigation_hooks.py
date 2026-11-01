"""Runtime-hook tests for the investigation mechanism (spec §3 H1/H2 + §6 R1).

Deterministic, tmp-DB only: a SimpleNamespace stands in for the agent (hooks
read ``_session_db`` / ``session_id`` through getattr), the DB is real.
Pins: capture happens once per durable delivery (dedup), probe binding is a
structural echo only, the ``investigation`` tool works through the inline
executor path (registry dispatch carries no db — spec R1), and every hook is
fail-open.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent import investigation_hooks
from hermes_state import SessionDB
from hermes_state_investigations import InvestigationMixin


@pytest.fixture()
def db(tmp_path):
    instance = SessionDB(db_path=tmp_path / "state.db")
    instance.ensure_session("sess-parent", source="test", session_key="k-parent")
    yield instance
    instance.close()


@pytest.fixture()
def agent(db):
    return SimpleNamespace(_session_db=db, session_id="sess-parent")


def _open_inv(db, with_probe=True):
    iid, _ = db.open_investigation(db.new_investigation_id(), "sess-parent", "why stale?")
    h1 = db.add_hypothesis(iid, "H1")
    pid = None
    if with_probe:
        pid = db.add_probe(iid, "check eviction log")
        db.add_prediction(pid, h1, "log line missing")
    return iid, pid


class TestH2Capture:
    def test_captured_once_and_deduped_on_redelivery(self, db, agent):
        iid, _pid = _open_inv(db)
        first = investigation_hooks.capture_tool_result(
            agent, "terminal", {}, "call-1", {"content": "no eviction line"})
        assert first is not None
        dup = investigation_hooks.capture_tool_result(
            agent, "terminal", {}, "call-1", {"content": "no eviction line"})
        assert dup is None  # same call_id = one observation, however re-delivered
        obs = db.list_observations(iid)
        assert len(obs) == 1 and obs[0]["assessed"] == 0

    def test_probe_binds_only_structural_echo(self, db, agent):
        iid, pid = _open_inv(db)
        # no probe_id in args -> unbound capture
        investigation_hooks.capture_tool_result(
            agent, "terminal", {}, "call-a", {"content": "x"})
        unbound = [o for o in db.list_observations(iid) if o["probe_id"] is None]
        assert len(unbound) == 1
        # valid echo -> bound, probe flips to observed
        investigation_hooks.capture_tool_result(
            agent, "terminal", {"probe_id": pid}, "call-b", {"content": "y"})
        bound = [o for o in db.list_observations(iid) if o["probe_id"] == pid]
        assert len(bound) == 1 and db.get_probe(pid)["status"] == "observed"
        # foreign/bogus id -> unbound, never crashes
        investigation_hooks.capture_tool_result(
            agent, "terminal", {"probe_id": "prb-not-real"}, "call-c", {"content": "z"})
        assert len([o for o in db.list_observations(iid) if o["probe_id"] == "prb-not-real"]) == 0

    def test_investigation_tool_self_calls_are_not_evidence(self, db, agent):
        iid, _ = _open_inv(db)
        assert investigation_hooks.capture_tool_result(
            agent, "investigation", {"op": "assess"}, "call-x", {"content": "{\"ok\":true}"}) is None
        assert db.list_observations(iid) == []

    def test_no_open_investigation_is_noop(self, db, agent):
        assert investigation_hooks.capture_tool_result(
            agent, "terminal", {}, "call-9", {"content": "x"}) is None

    def test_fail_open_on_broken_store(self, agent):
        broken = SimpleNamespace(_session_db=object(), session_id="s")
        assert investigation_hooks.capture_tool_result(
            broken, "terminal", {}, "c", {"content": "x"}) is None


class TestR1InlineWiring:
    def test_investigation_resolves_through_inline_executors(self):
        from agent.inline_tool_executors import INLINE_TOOL_EXECUTORS

        assert "investigation" in INLINE_TOOL_EXECUTORS

    def test_inline_executor_end_to_end(self, db, agent):
        from agent.inline_tool_executors import INLINE_TOOL_EXECUTORS

        out = json.loads(INLINE_TOOL_EXECUTORS["investigation"](
            agent, {"op": "open", "question": "Q?", "hypotheses": ["Ha", "Hb"]},
            SimpleNamespace(effective_task_id=None)))
        assert out["ok"] and out["investigation_id"].startswith("inv")
        assert db.get_open_investigation("sess-parent")["question"] == "Q?"

    def test_registry_handler_without_db_reports_unavailable(self):
        # documents WHY the inline path is mandatory: the registry handler gets no db.
        from tools.investigation_tool import investigation

        out = json.loads(investigation({"op": "open", "question": "x"},
                                       db=None, current_session_id="s"))
        assert out["ok"] is False and "unavailable" in out["error"]


class TestH1Render:
    def test_note_shows_question_and_unassessed(self, db, agent):
        iid, _pid = _open_inv(db)
        oid = db.capture_observation(iid, dedup_key="k-r", source="tool", content="z")
        note = investigation_hooks.render_note(agent)
        assert note and "why stale?" in note and "UNASSESSED" in note
        assert oid in note

    def test_silent_without_investigation(self, agent):
        assert investigation_hooks.render_note(agent) is None

    def test_fail_open_on_broken_store(self, agent):
        broken = SimpleNamespace(_session_db=object(), session_id="s")
        assert investigation_hooks.render_note(broken) is None

    def test_wired_into_request_assembly(self):
        # The row lands in the API copy only — pinned at the source level: the
        # hook exists in turn_request_assembly and appends after user-merge.
        import inspect

        import agent.turn_request_assembly as tra

        src = inspect.getsource(tra)
        hook = src.index("INVESTIGATION-HOOK: H1")
        merge = src.index("_drop_thinking_only_and_merge_users")
        norm = src.index("# Normalize whitespace and tool-call JSON")
        assert merge < hook < norm


class TestH4StopGate:
    """The gate itself inside apply_stop_gates: fires once, only when the store
    says observations are unassessed, and its nudge row carries the synthetic flag."""

    def _agent_stub(self, db, monkeypatch):
        import agent.turn_stop_gates as tsg

        for name in ("_late_lsp_verdicts", "_verify_on_stop_nudge", "_pre_verify_nudge"):
            monkeypatch.setattr(tsg, name, lambda *a, **k: None)
        monkeypatch.setattr(tsg, "_kanban_stop_nudge", lambda *a, **k: None)
        return SimpleNamespace(
            _session_db=db, session_id="sess-parent", _session_messages=None,
            _interim_content_was_streamed=lambda text: False,
            _flush_messages_to_session_db=lambda *a, **k: None,
            _emit_interim_assistant_message=lambda *a, **k: None,
            _emit_diagnostic_status=lambda *a, **k: None,
        )

    def test_gate_nudges_once_then_stays_quiet(self, db, agent, monkeypatch):
        import agent.turn_stop_gates as tsg

        iid, _pid = _open_inv(db)
        db.capture_observation(iid, dedup_key="k-g", source="tool", content="r")
        stub = self._agent_stub(db, monkeypatch)
        messages: list[dict] = []
        final_msg = {"role": "assistant", "content": "answer"}
        verdict = tsg.apply_stop_gates(
            stub, final_msg, final_response="answer", messages=messages,
            conversation_history=None, pending_verification_response=None,
            pending_verification_response_previewed=None)
        assert verdict.continue_turn is True
        assert final_msg["finish_reason"] == "investigation_unassessed"
        nudge = messages[-1]
        assert nudge["role"] == "user" and nudge["_investigation_stop_synthetic"] is True
        # second attempt in the same turn: nudged once, then the turn may end
        verdict2 = tsg.apply_stop_gates(
            stub, final_msg, final_response="answer", messages=messages,
            conversation_history=None, pending_verification_response=None,
            pending_verification_response_previewed=None)
        assert verdict2.continue_turn is False

    def test_gate_silent_when_assessed(self, db, monkeypatch):
        iid, _pid = _open_inv(db)
        oid = db.capture_observation(iid, dedup_key="k-h", source="tool", content="r")
        db.assess_observation(oid, "supports H1")
        import agent.turn_stop_gates as tsg

        stub = self._agent_stub(db, monkeypatch)
        messages: list[dict] = []
        verdict = tsg.apply_stop_gates(
            stub, {"role": "assistant", "content": "a"}, final_response="a",
            messages=messages, conversation_history=None,
            pending_verification_response=None,
            pending_verification_response_previewed=None)
        assert verdict.continue_turn is False and messages == []


class TestForkStampAndJoinCapture:
    """H3 both halves at the store level: fork declares the probe (structural
    bind + meta stamp), join delivers the note (one branch-deduped observation
    in the investigation the probe belongs to)."""

    def _fork_with_probe(self, db):
        from tools.session_fork_tool import session_fork
        import json as _json

        db.append_messages_batch("sess-parent", [dict(role="user", content="seed")])
        iid, pid = _open_inv(db)
        out = _json.loads(session_fork(name="check eviction", probe_id=pid,
                                       db=db, current_session_id="sess-parent"))
        return iid, pid, out

    def test_fork_binds_probe_and_stamps_meta(self, db):
        iid, pid, out = self._fork_with_probe(db)
        assert out["ok"] and out["investigation"]["investigation_id"] == iid
        assert db.get_probe(pid)["branch_id"] == out["branch_id"]
        branch = db.get_branch(out["branch_id"])
        meta = __import__("json").loads(branch["meta_json"])
        assert meta["investigation_id"] == iid and meta["probe_id"] == pid

    def test_fork_without_or_bogus_probe_stays_plain(self, db):
        from tools.session_fork_tool import session_fork
        import json as _json

        db.append_messages_batch("sess-parent", [dict(role="user", content="seed")])
        _open_inv(db)
        out = _json.loads(session_fork(name="plain", probe_id="prb-ghost",
                                       db=db, current_session_id="sess-parent"))
        assert out["ok"] and "investigation" not in out

    def test_join_note_lands_as_one_deduped_branch_observation(self, db):
        import asyncio

        iid, pid, out = self._fork_with_probe(db)
        branch = db.get_branch(out["branch_id"])
        branch["meta_json"] = __import__("json").dumps(
            __import__("json").loads(branch["meta_json"]))
        note = "[work-note from branch check eviction] no eviction line found"

        async def run():
            await investigation_hooks.capture_branch_join(db, branch, "note", note)
            # second delivery of the SAME join: dedup key says once
            await investigation_hooks.capture_branch_join(db, branch, "note", note)

        asyncio.run(run())
        obs = [o for o in db.list_observations(iid) if o["source"] == "branch"]
        assert len(obs) == 1 and obs[0]["probe_id"] == pid

    def test_join_without_stamp_records_nothing(self, db):
        import asyncio

        _open_inv(db)
        branch = {"branch_id": "b9", "parent_session_id": "sess-parent", "meta_json": "{}"}
        asyncio.run(investigation_hooks.capture_branch_join(db, branch, "note", "x"))
        # no stamped investigation: no observations anywhere
        assert db.list_observations(db.get_open_investigation("sess-parent")["investigation_id"]) == []

    def test_gateway_join_and_boot_wiring_guards(self):
        import inspect

        import gateway.run_startup as rs
        import gateway.slash_commands_fork as sc

        assert "INVESTIGATION-HOOK: H3" in inspect.getsource(sc.join_branch if hasattr(sc, "join_branch") else sc)
        src = inspect.getsource(rs)
        assert "INVESTIGATION-HOOK: boot settle" in src
        # settle must run BEFORE the resume scheduler consumes candidates
        assert src.index("INVESTIGATION-HOOK: boot settle") < src.index("_schedule_resume_pending_sessions()",
                                                                        src.index("INVESTIGATION-HOOK: boot settle") - 2000)


class TestH4Helpers:
    def test_unassessed_ids_and_nudge(self, db, agent):
        iid, _ = _open_inv(db)
        assert investigation_hooks.stop_nudge(agent) is None
        db.capture_observation(iid, dedup_key="k1", source="tool", content="y")
        nudge = investigation_hooks.stop_nudge(agent)
        assert nudge is not None and "assess" in nudge

    def test_nudge_silent_without_investigation(self, agent):
        assert investigation_hooks.stop_nudge(agent) is None


class TestMixinBootSettle:
    def test_unknown_sessions_listed(self, db):
        iid, pid = _open_inv(db)
        db.set_probe_status(pid, "dispatched")
        assert InvestigationMixin is not None
        db.settle_unresolved_probes()
        sessions = db.list_open_investigation_unknown_sessions()
        assert sessions == ["sess-parent"]
        # after the evidence is accounted and the investigation closes, no wake is owed
        oid = db.capture_observation(iid, dedup_key="k9", source="tool", content="z")
        db.assess_observation(oid, "inconclusive: не разделяет H1/H2")
        assert db.close_investigation(iid, "done", "none")["closed"] is True
        assert db.list_open_investigation_unknown_sessions() == []

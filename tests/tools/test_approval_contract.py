"""Approval contract B2 tests (TZ-2026-11-16-dsh-B).

B2: allowed-once grants, the pending TTL, and the three stable denial
codes — behind ``approvals.once_and_ttl`` (default OFF: legacy behavior is
byte-identical). Acceptance from the TZ:

* ``ttl=1`` reproduces ``APPROVAL_CANCELLED``;
* ``/deny``, timeout, and "no one to ask" give three different traces in
  the ledger;
* the existing ledger / exec-ask-leak tests stay green (flag off).
"""

import json
import time

import pytest

import tools.approval as A
import tools.approval_contract as AC
import tools.approval_ledger as L


def _reset_process():
    A._pending.clear()
    A._gateway_queues.clear()
    A._session_approved.clear()


def _rows(session_key):
    import sqlite3

    from hermes_constants import get_hermes_home

    conn = sqlite3.connect(get_hermes_home() / "state.db")
    try:
        return conn.execute(
            "SELECT state, resolution, command FROM pending_approvals"
            " WHERE session_key=? ORDER BY created_at",
            (session_key,),
        ).fetchall()
    finally:
        conn.close()


def _extra_json(session_key, command):
    import sqlite3

    from hermes_constants import get_hermes_home

    conn = sqlite3.connect(get_hermes_home() / "state.db")
    try:
        row = conn.execute(
            "SELECT extra_json FROM pending_approvals"
            " WHERE session_key=? AND command=?",
            (session_key, command),
        ).fetchone()
        return json.loads(row[0]) if row else {}
    finally:
        conn.close()


def _b2_ledger(monkeypatch, ttl: int = 3600):
    """The B2 ledger paths: feature on, B2 active, B2 TTL in seconds."""
    monkeypatch.setattr(L, "_enabled", lambda: True)
    monkeypatch.setattr(L, "_b2_active", lambda: True)
    monkeypatch.setattr(L, "_b2_ttl", lambda: ttl)
    monkeypatch.setattr(A, "_b2_active", lambda: True)


# --- the contract itself -----------------------------------------------------

class TestStableCodes:
    def test_codes_are_stable_strings(self):
        assert AC.APPROVAL_REJECTED == "APPROVAL_REJECTED"
        assert AC.APPROVAL_CANCELLED == "APPROVAL_CANCELLED"
        assert AC.APPROVAL_UNAVAILABLE == "APPROVAL_UNAVAILABLE"

    def test_gate_code_mapping(self):
        assert AC.gate_code("denied") == AC.APPROVAL_REJECTED
        assert AC.gate_code("timeout") == AC.APPROVAL_CANCELLED
        assert AC.gate_code("cancelled") == AC.APPROVAL_CANCELLED
        assert AC.gate_code("notify_failed") == AC.APPROVAL_UNAVAILABLE
        # anything that ended without a surface or an answer is UNAVAILABLE
        assert AC.gate_code("some_new_outcome") == AC.APPROVAL_UNAVAILABLE

    def test_structured_answer_requires_a_reason(self):
        with pytest.raises(ValueError):
            AC.structured_answer("deny", "")
        with pytest.raises(ValueError):
            AC.structured_answer("deny", "   ")
        assert AC.structured_answer("deny", "no force push here") == {
            "decision": "deny", "reason": "no force push here", "scope": "once"}

    def test_only_once_scope_is_in_the_contract(self):
        with pytest.raises(ValueError):
            AC.structured_answer("allow", "ok", scope="always")
        with pytest.raises(ValueError):
            AC.structured_answer("allow", "ok", scope="session")

    def test_unknown_decision_rejected(self):
        with pytest.raises(ValueError):
            AC.structured_answer("maybe", "x")

    def test_error_meta_is_structural(self):
        assert AC.error_meta(AC.APPROVAL_REJECTED) == {"error": {"code": "APPROVAL_REJECTED"}}
        assert AC.error_meta(AC.APPROVAL_REJECTED, "user reason") == {
            "error": {"code": "APPROVAL_REJECTED", "reason": "user reason"}}

    def test_tag_b2_flag_off_is_a_noop(self, monkeypatch):
        monkeypatch.setattr(A, "_b2_active", lambda: False)
        d = {"approved": False}
        out = A._tag_b2(d, "denied", "some reason")
        assert out is d
        assert "approval_code" not in d

    def test_tag_b2_flag_on_tags_code_and_reason(self, monkeypatch):
        monkeypatch.setattr(A, "_b2_active", lambda: True)
        out = A._tag_b2({"approved": False}, "denied", "user reason")
        assert out["approval_code"] == AC.APPROVAL_REJECTED
        assert out["approval_reason"] == "user reason"
        out2 = A._tag_b2({}, "timeout")
        assert out2["approval_code"] == AC.APPROVAL_CANCELLED
        assert "approval_reason" not in out2

    def test_pending_ttl_config_reader(self, monkeypatch):
        monkeypatch.setattr(AC, "_approval_config", dict)
        assert AC.pending_ttl_s() == 3600  # default
        monkeypatch.setattr(AC, "_approval_config", lambda: {"pending_ttl_s": 0})
        assert AC.pending_ttl_s() == 0     # 0 = never expires
        monkeypatch.setattr(AC, "_approval_config", lambda: {"pending_ttl_s": 7})
        assert AC.pending_ttl_s() == 7


# --- the ledger traces (TZ acceptance: three different traces) ---------------

class TestLedgerTraces:
    def test_deny_resolves_with_rejected_code(self, monkeypatch):
        _b2_ledger(monkeypatch)
        _reset_process()
        sk = "b2-trace-deny"
        A.submit_pending(sk, {"command": "git push --force", "pattern_key": "force"})
        resolved = L.resolve_pending(sk, "deny", reason="not now")
        assert len(resolved) == 1
        assert _rows(sk) == [("denied", AC.APPROVAL_REJECTED, "git push --force")]

    def test_timeout_row_auto_cancels_with_code(self, monkeypatch):
        _b2_ledger(monkeypatch, ttl=1)
        _reset_process()
        sk = "b2-trace-timeout"
        A.submit_pending(sk, {"command": "rm -rf /", "pattern_key": "rm"})
        time.sleep(1.15)  # nobody answered in the window
        assert L.list_pending(sk) == []
        assert _rows(sk) == [("cancelled", AC.APPROVAL_CANCELLED, "rm -rf /")]
        # a later /approve cannot revive a cancelled row
        assert L.resolve_pending(sk, "once") == []

    def test_no_surface_trace_carries_unavailable_code(self, monkeypatch):
        _b2_ledger(monkeypatch)
        _reset_process()
        sk = "b2-trace-unavail"
        A.submit_pending(sk, {"command": "kubectl delete ns prod", "pattern_key": "kubectl",
                              "approval_code": AC.APPROVAL_UNAVAILABLE})
        extra = _extra_json(sk, "kubectl delete ns prod")
        assert extra.get("approval_code") == AC.APPROVAL_UNAVAILABLE
        # the row itself stays pending (a human may still review it in /approve)
        assert [r[0] for r in _rows(sk)] == ["pending"]

    def test_ttl_1_reproduces_cancelled(self, monkeypatch):
        """TZ acceptance, verbatim: test ``ttl=1`` reproduces APPROVAL_CANCELLED.
        The auto-resolution is lazy: it runs on the next ledger read (no
        scheduler), so the assertion goes through the read path first."""
        _b2_ledger(monkeypatch, ttl=1)
        _reset_process()
        sk = "b2-ttl1"
        A.submit_pending(sk, {"command": "dropdb prod", "pattern_key": "dropdb"})
        time.sleep(1.15)
        assert L.list_pending(sk) == []  # the read path GC'd the row
        assert _rows(sk) == [("cancelled", AC.APPROVAL_CANCELLED, "dropdb prod")]

    def test_ttl_zero_never_expires(self, monkeypatch):
        _b2_ledger(monkeypatch, ttl=0)
        _reset_process()
        sk = "b2-forever"
        A.submit_pending(sk, {"command": "echo forever", "pattern_key": "echo"})
        import sqlite3

        from hermes_constants import get_hermes_home
        conn = sqlite3.connect(get_hermes_home() / "state.db")
        try:
            expires = conn.execute(
                "SELECT expires_at FROM pending_approvals WHERE session_key=?", (sk,)).fetchone()[0]
        finally:
            conn.close()
        assert expires == 0
        time.sleep(0.1)
        assert L.list_pending(sk)  # still pending: the GC skips expires_at=0

    def test_legacy_resolution_keeps_raw_choice(self, monkeypatch):
        """Flag off: the resolution column keeps the raw choice word (legacy)."""
        monkeypatch.setattr(L, "_enabled", lambda: True)
        monkeypatch.setattr(L, "_b2_active", lambda: False)
        _reset_process()
        sk = "b2-legacy"
        A.submit_pending(sk, {"command": "legacy", "pattern_key": "pk"})
        L.resolve_pending(sk, "deny")
        assert _rows(sk) == [("denied", "deny", "legacy")]


# --- the structured answer at the gateway boundary (B2a) ---------------------

class TestStructuredGatewayAnswers:
    def test_bare_deny_rejected_without_reason(self, monkeypatch):
        _b2_ledger(monkeypatch)
        _reset_process()
        with pytest.raises(ValueError, match="reason"):
            A.resolve_gateway_approval("b2-struct", "deny", reason=None)

    def test_deny_with_reason_passes(self, monkeypatch):
        _b2_ledger(monkeypatch)
        _reset_process()
        assert A.resolve_gateway_approval("b2-struct2", "deny", reason="no") >= 0

    def test_persistent_scopes_out_of_contract(self, monkeypatch):
        _b2_ledger(monkeypatch)
        _reset_process()
        with pytest.raises(ValueError, match="once"):
            A.resolve_gateway_approval("b2-struct3", "session", reason="ok")
        with pytest.raises(ValueError, match="once"):
            A.resolve_gateway_approval("b2-struct4", "always", reason="ok")
        # the allowed-once grant is in the contract
        assert A.resolve_gateway_approval("b2-struct5", "once", reason="ok") >= 0

    def test_legacy_surface_bypasses_the_contract(self, monkeypatch):
        """``structured=False`` (a card tap) never enforces the contract."""
        _b2_ledger(monkeypatch)
        _reset_process()
        assert A.resolve_gateway_approval("b2-card", "deny", structured=False) >= 0


# --- B2c: no one to ask ------------------------------------------------------

class TestNoSurfaceDenial:
    def test_pending_result_denies_upfront_with_code(self, monkeypatch):
        _b2_ledger(monkeypatch)
        _reset_process()
        sk = "b2-nosurface"
        res = A._pending_result(
            A._COMMAND_GATE, sk,
            command="rm -rf /", description="deletes the root",
            pattern_key="rm-rf", pattern_keys=["rm-rf"], body=None, smart_denied=False,
        )
        assert res["approved"] is False
        assert res["status"] == "blocked"
        assert res["approval_code"] == AC.APPROVAL_UNAVAILABLE
        # the ledger kept the trace
        assert _extra_json(sk, "rm -rf /").get("approval_code") == AC.APPROVAL_UNAVAILABLE

    def test_pending_result_legacy_shape_untouched(self, monkeypatch):
        """Flag off: the pending_approval shape is byte-identical (no code key)."""
        monkeypatch.setattr(L, "_enabled", lambda: True)
        monkeypatch.setattr(L, "_b2_active", lambda: False)
        monkeypatch.setattr(A, "_b2_active", lambda: False)
        _reset_process()
        sk = "b2-nosurface-legacy"
        res = A._pending_result(
            A._COMMAND_GATE, sk,
            command="rm -rf /", description="deletes the root",
            pattern_key="rm-rf", pattern_keys=["rm-rf"], body=None, smart_denied=False,
        )
        assert res["status"] == "pending_approval"
        assert "approval_code" not in res


# --- terminal meta envelope (B2d) --------------------------------------------

class TestTerminalMeta:
    def test_meta_only_when_the_flag_tagged_the_result(self):
        """The terminal envelope folds the code into meta.error.* only when
        the gate result carries it (i.e. the flag is on) — legacy envelopes
        stay byte-identical."""
        from tools import terminal_tool as TT

        approval_tagged = {
            "approved": False,
            "message": "BLOCKED: Command denied.",
            "approval_code": AC.APPROVAL_REJECTED,
            "approval_reason": "user said no",
        }
        _extra = {}
        if approval_tagged.get("user_summary"):
            _extra["user_summary"] = approval_tagged["user_summary"]
        if approval_tagged.get("approval_code"):
            _extra["meta"] = AC.error_meta(
                approval_tagged["approval_code"], approval_tagged.get("approval_reason", ""))
        body = TT._error_json(approval_tagged.get("message", "x"), status="blocked", **_extra)
        assert json.loads(body)["meta"]["error"]["code"] == "APPROVAL_REJECTED"
        assert json.loads(body)["meta"]["error"]["reason"] == "user said no"

    def test_legacy_envelope_has_no_meta(self):
        from tools import terminal_tool as TT

        approval_legacy = {"approved": False, "message": "BLOCKED: Command denied."}
        _extra = {}
        if approval_legacy.get("user_summary"):
            _extra["user_summary"] = approval_legacy["user_summary"]
        if approval_legacy.get("approval_code"):
            _extra["meta"] = AC.error_meta(approval_legacy["approval_code"])
        body = TT._error_json(approval_legacy.get("message", "x"), status="blocked", **_extra)
        assert "meta" not in json.loads(body)

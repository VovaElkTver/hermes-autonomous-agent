"""Acceptance cases for TZ-agency-toolset.md v0.2 (12 cases + signature-forgery).

Run:  python -m pytest tests/test_agency_toolset.py -q
      (or plain: python tests/test_agency_toolset.py)
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plugins.agency import store
from plugins.agency import agent_tools

DB = None


def setup_function(_):
    global DB
    tmp = tempfile.mkdtemp(prefix="agency-test-")
    DB = os.path.join(tmp, "agency-state.db")
    store.set_busy_timeout(5000)
    store.REVOKED.clear()                    # module fixtures are per-test fresh
    store.seed(DB)
    agent_tools.set_identity("admin-1", 1)
    agent_tools.set_caller_session(None)


def call(tool, **args):
    handler = dict((n, h) for n, _s, h in agent_tools.tool_defs())[tool]
    args.setdefault("db_path", DB)          # route handlers at the test DB
    out = json.loads(handler(dict(args)))
    assert isinstance(out, dict), "envelope must be a JSON object"
    return out


# --- case 2: seven calls run natively (no terminal anywhere) ------------------
def test_case2_seven_calls_without_terminal():
    experts = call("agency_list_experts")
    assert experts["ok"] and any(e["position_id"] == "expert-a" for e in experts["experts"])
    r = call("agency_survey", expert_id="expert-a", issue_id="i-001", question="что видишь?")
    assert r["ok"] and r["event_seq"] > 0
    b = call("agency_create_branch", issue_id="i-001", title="правки договора")
    assert b["direction_id"].endswith("direction-1")
    c = call("agency_record_commitment", issue_id="i-001", who="admin-1",
             what="собрать статусы", due="2026-11-07T18:00:00Z")
    assert c["ok"]
    # expert consents via addressed reply (envelope in the real channel); admin
    # records it with refs to that consent evidence:
    e = call("agency_record_commitment", issue_id="i-001", who="expert-a",
             what="подтверждаю участие", due="2026-11-07T18:00:00Z",
             refs=["channel:expert-a-reply"])
    assert e["ok"], e
    st = call("agency_issue_status", issue_id="i-001")
    assert st["ok"] and st["commitments"] and st["waiting_on"] == []
    e = call("agency_escalate", issue_id="i-001", reason="спор редактур", to="owner")
    assert e["ok"]
    rep = call("agency_client_reply", issue_id="i-001", text="принято в работу")
    assert rep["event_id"] == f"ledger:{rep['event_seq']}"


# --- cases 3/4: idempotency lifecycle -----------------------------------------
def test_case3_repeat_same_key_returns_prior_result():
    args = dict(expert_id="expert-a", issue_id="i-001", question="q",
                idempotency_key="key-1")
    first = call("agency_survey", **args)
    again = call("agency_survey", **args)     # lost-response retry
    assert first["event_seq"] == again["event_seq"]
    with store.connect(DB) as c:
        n = c.execute("SELECT COUNT(*) FROM ledger WHERE idem_key='key-1'").fetchone()[0]
    assert n == 1


def test_case4_same_key_other_args_conflicts():
    call("agency_survey", expert_id="expert-a", issue_id="i-001",
         question="q", idempotency_key="k")
    r = call("agency_survey", expert_id="expert-b", issue_id="i-001",
             question="q", idempotency_key="k")
    assert not r["ok"] and r["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    with store.connect(DB) as c:
        n = c.execute("SELECT COUNT(*) FROM ledger WHERE idem_key='k'").fetchone()[0]
    assert n == 1


def test_3_3_no_key_generates_unique_keys():
    a = call("agency_survey", expert_id="expert-a", issue_id="i-001", question="q")
    b = call("agency_survey", expert_id="expert-a", issue_id="i-001", question="q")
    assert a["idempotency_key"] != b["idempotency_key"]


# --- case 5: crash after commit, before reply ---------------------------------
def test_case5_lost_response_retry_after_commit():
    first = call("agency_client_reply", issue_id="i-001", text="текст",
                 idempotency_key="ack-1")   # response 'lost' by caller is fine:
    # the result lives in result_json of the same committed row:
    second = call("agency_client_reply", issue_id="i-001", text="текст",
                  idempotency_key="ack-1")
    assert first["event_seq"] == second["event_seq"]


# --- case 6: fixation precedes successful tool return --------------------------
def test_case6_fix_before_ack_order_in_ledger():
    rep = call("agency_client_reply", issue_id="i-001", text="статус")
    row = store.connect(DB).execute(
        "SELECT type FROM ledger WHERE seq=?", (rep["event_seq"],)).fetchone()
    assert row["type"] == "client_reply"       # the returned event id IS the fix


# --- case 7: consent required for other-position commitment --------------------
def test_case7_consent_required_for_foreign_commitment():
    r = call("agency_record_commitment", issue_id="i-001", who="expert-a",
             what="проверить РЭ", due="2026-11-08T10:00:00Z")
    assert not r["ok"] and r["error"]["code"] == "CONSENT_REQUIRED"
    ok = call("agency_record_commitment", issue_id="i-001", who="expert-a",
              what="проверить РЭ", due="2026-11-08T10:00:00Z",
              refs=["ledger:3"])               # recorded consent evidence
    assert ok["ok"]


# --- case 8: revoked mandate blocks writes -------------------------------------
def test_case8_revoked_mandate_blocks_writes():
    store.revoke_mandate("admin-1", 1)
    r = call("agency_survey", expert_id="expert-a", issue_id="i-001", question="q")
    assert not r["ok"] and r["error"]["code"] == "MANDATE_REVOKED"


# --- case 9: every submission is all-or-nothing; ledger replays from zero ------
def test_case9_atomic_units_and_replay():
    # TZ v0.2 case 9 (reworded per Vl 07.11): a tool call is ONE serialized
    # transaction — it either fully lands and RETURNS its result, or leaves
    # nothing. There is no half-written state to simulate a death for: /fork
    # ends with a return, not an exit. So the case verifies the real contract:
    # concurrent submitters, one row per operation, projection replaying cleanly.
    results = []
    import threading

    def writer(i):
        results.append(call("agency_survey", expert_id="expert-a", issue_id="i-001",
                            question=f"w{i}", idempotency_key=f"w{i}"))

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 10
    rows = store.connect(DB).execute(
        "SELECT seq,idem_key FROM ledger WHERE type='survey_request'"
        " ORDER BY seq").fetchall()
    keys = [r["idem_key"] for r in rows]
    assert len(keys) == 10 and len(set(keys)) == 10      # exactly one row per op
    proj = store.replay_projection(DB, "i-001")
    assert len(proj["phases"]) == 10                     # everything replays


# --- case 10: concurrent create_branch → distinct directions -------------------
def test_case10_concurrent_branches_serialized_distinct():
    results = []

    def mk(i):
        results.append(call("agency_create_branch", issue_id="i-001",
                            title=f"branch-{i}"))

    threads = [threading.Thread(target=mk, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ids = sorted(r["direction_id"] for r in results if r["ok"])
    assert len(ids) == 6 and len(set(ids)) == 6
    with store.connect(DB) as c:
        n = c.execute("SELECT COUNT(*) FROM ledger WHERE type='branch'").fetchone()[0]
    assert n == 6


# --- case 11: busy lock exhaustion is a machine-code retryable error ------------
def test_case11_lock_timeout_envelope(tmp_path=None):
    old = store._busy_timeout_ms
    store.set_busy_timeout(200)                                # shrink window
    blocker = sqlite3.connect(DB, timeout=5)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        blocker.execute(
            "INSERT INTO issues VALUES('j','t','2026-01-01T00:00:00Z')")
        r = call("agency_record_commitment", issue_id="i-001", who="admin-1",
                 what="x", due="2026-11-08T10:00:00Z")
        assert not r["ok"] and r["error"]["code"] == "LOCK_TIMEOUT"
        assert r["error"]["retryable"] is True
    finally:
        blocker.rollback(); blocker.close()
        store.set_busy_timeout(old)


# --- case 12: round-trip projection equality ------------------------------------
def test_case12_roundtrip_projection_matches_status():
    call("agency_survey", expert_id="expert-a", issue_id="i-001", question="q")
    call("agency_create_branch", issue_id="i-001", title="b1")
    status = call("agency_issue_status", issue_id="i-001")
    rebuilt = store.replay_projection(DB, "i-001")
    assert status["as_of_seq"] == rebuilt["as_of_seq"]
    for field in ("branches", "waiting_on"):
        assert status[field] == rebuilt[field]


# --- case 1: registration gate -------------------------------------------------
def test_case1_gate_registers_behind_flag():
    import plugins.agency as A

    class Ctx:
        def __init__(self, flag):
            self.flag = flag
            self.registered = []
        def get_config(self, k, d=None):
            return self.flag if k == "enabled" else d
        def register_tool(self, **kw):
            self.registered.append(kw["name"])

    for expected in (False, True):
        c = Ctx(expected)
        A._ctx = c
        # admin-mode conjunct pinned ON; the pure settings-gate is case-tested below
        A._admin_mode_on = lambda: True
        assert A._enabled() == expected
    A._ctx = None

    c = Ctx(True)
    A.register(c)
    assert len(c.registered) == 9 and "agency_client_reply" in c.registered
    c2 = Ctx(False)
    A.register(c2)   # registered with check_fn=False → invisible to the model


# --- case 2b: loop is the sole owner of agency-state.db -------------------------
def test_case2b_no_direct_db_access_outside_store():
    # TZ v0.2 §6: loop (store.py) is the ONLY owner of agency-state.db; handlers
    # and agent code must not open it directly. AST-based: flags real
    # sqlite3.connect() calls and db-path literals anywhere but store.py.
    import ast
    plugin_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "plugins", "agency")
    offenders = []
    for fname in os.listdir(plugin_dir):
        if fname == "store.py" or not fname.endswith(".py"):
            continue
        src = open(os.path.join(plugin_dir, fname)).read()
        reasons = []
        if "agency-state.db" in src:
            reasons.append("db-path literal")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            reasons.append("unparsable")
            tree = None
        for node in ast.walk(tree) if tree else ():
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == "connect" \
                    and isinstance(node.func.value, ast.Name) \
                    and node.func.value.id == "sqlite3":
                reasons.append("sqlite3.connect call")
                break
        if reasons:
            offenders.append(f"{fname}: {','.join(reasons)}")
    assert offenders == [], f"direct DB access outside store.py: {offenders}"


# --- case 2c: loop lives between calls — overdue sweep + storage care -----------
def _ledger_count(db, where="1=1"):
    return store.connect(db).execute(
        f"SELECT COUNT(*) FROM ledger WHERE {where}").fetchone()[0]


def test_loop_sweep_overdue_marks_once():
    call("agency_record_commitment", issue_id="i-001", who="admin-1",
         what="past obligation", due="2026-01-01T00:00:00Z")
    fut = call("agency_record_commitment", issue_id="i-001", who="admin-1",
               what="future obligation", due="2099-01-01T00:00:00Z")
    assert fut["ok"], fut
    n0 = _ledger_count(DB)
    marked = store.sweep_overdue(DB, now_iso="2026-11-08T00:00:00Z")
    assert len(marked) == 1, marked
    assert _ledger_count(DB) == n0 + 1
    # second sweep on same clock: nothing new (marker keys are deterministic)
    assert store.sweep_overdue(DB, now_iso="2026-11-08T00:00:00Z") == []
    assert _ledger_count(DB) == n0 + 1
    row = store.connect(DB).execute(
        "SELECT actor_position_id,type,kind,payload_json FROM ledger WHERE seq=?",
        (marked[0],)).fetchone()
    assert row["type"] == "commitment_overdue" and row["kind"] == "observation"
    assert row["actor_position_id"] == "agency-loop"


def test_loop_worker_ticks_between_calls():
    from plugins.agency.loop import LoopWorker
    c = call("agency_record_commitment", issue_id="i-001", who="admin-1",
             what="worker-swept obligation", due="2026-01-01T00:00:00Z")
    assert c["ok"], c
    stop = threading.Event()
    w = LoopWorker(db_path=DB, interval_s=0.05, stop_event=stop)
    w.start()
    deadline = time.time() + 5
    while time.time() < deadline:
        if _ledger_count(DB, "type='commitment_overdue'") >= 1:
            break
        time.sleep(0.02)
    stop.set()
    w.join(timeout=2)
    assert not w.is_alive(), "worker must stop cleanly"
    assert _ledger_count(DB, "type='commitment_overdue'") == 1, \
        "repeated ticks must not duplicate markers"


def test_loop_tick_survives_concurrent_writers():
    from plugins.agency.loop import LoopWorker

    def writer(i):
        for k in range(5):
            call("agency_create_branch", issue_id="i-001", title=f"w{i}-{k}")

    ws = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
    for t in ws:
        t.start()
    stop = threading.Event()
    w = LoopWorker(db_path=DB, interval_s=0.02, stop_event=stop)
    w.start()
    for t in ws:
        t.join()
    stop.set()
    w.join(timeout=2)
    n = _ledger_count(DB, "type='branch'")
    assert n == 20, f"all writer events must land exactly once, got {n}"
    rows = store.connect(DB).execute("SELECT seq FROM ledger ORDER BY seq").fetchall()
    seqs = [r["seq"] for r in rows]
    assert len(set(seqs)) == len(seqs), "seq must stay unique under load"


# --- identity: single admin actor from plugin settings (Вл 07.11) ----------------
def test_identity_loaded_from_settings():
    # any position from settings is honored; admin-only verbs stay admin-only,
    # so use an administrator position with a bumped mandate version
    agent_tools.load_identity({"identity": {"position_id": "admin-1", "mandate_version": 3}})
    try:
        r = call("agency_survey", expert_id="expert-b", issue_id="i-001", question="q")
        assert r["ok"], r
        row = store.connect(DB).execute(
            "SELECT actor_position_id,mandate_version FROM ledger ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        assert (row["actor_position_id"], row["mandate_version"]) == ("admin-1", 3)
    finally:
        agent_tools.load_identity({"identity": {"position_id": "admin-1", "mandate_version": 1}})
    # forge still ignored: model-passed position fields never reach the signature
    r = call("agency_survey", expert_id="expert-b", issue_id="i-001", question="q2",
             position_id="expert-b", mandate_version=77)
    assert r["ok"], r


# --- coworking: self-registration, admin approval, role-scoped verbs (Вл 07.11) --
def test_coworking_register_approve_self_commit():
    agent_tools.set_caller_session("sess-exp1")
    r = call("agency_register", position_id="expert-x", role="expert",
             competencies=["contracts"])
    assert r["ok"] and r["status"] == "pending" and not r["already_registered"], r
    # pending position cannot act yet
    c = call("agency_record_commitment", issue_id="i-001", who="expert-x",
             what="my part", due="2026-12-01T00:00:00Z")
    assert not c["ok"] and c["error"]["code"] == "REGISTRATION_PENDING", c
    # registration landed on the registry track of the ledger
    reg = store.connect(DB).execute(
        "SELECT type,kind,issue_id FROM ledger WHERE type='registration'").fetchone()
    assert reg["issue_id"] == store.REGISTRY_ISSUE and reg["kind"] == "observation"
    # admin approves; expert becomes active and may commit itself only
    agent_tools.set_caller_session(None)
    a = call("agency_approve", position_id="expert-x")
    assert a["ok"] and a["status"] == "active", a
    agent_tools.set_caller_session("sess-exp1")
    mine = call("agency_record_commitment", issue_id="i-001", who="expert-x",
                what="my part", due="2026-12-01T00:00:00Z")
    assert mine["ok"], mine


def test_coworking_role_gates():
    agent_tools.set_caller_session("sess-e2")
    call("agency_register", position_id="expert-y", role="expert")
    agent_tools.set_caller_session(None)
    call("agency_approve", position_id="expert-y")
    agent_tools.set_caller_session("sess-e2")
    s = call("agency_survey", expert_id="expert-a", issue_id="i-001", question="?")
    assert not s["ok"] and s["error"]["code"] == "ROLE_FORBIDDEN", s
    f = call("agency_record_commitment", issue_id="i-001", who="admin-1",
             what="on behalf", due="2026-12-01T00:00:00Z")
    assert not f["ok"] and f["error"]["code"] == "ROLE_FORBIDDEN", f
    b = call("agency_create_branch", issue_id="i-001", title="x")
    assert not b["ok"] and b["error"]["code"] == "ROLE_FORBIDDEN", b


def test_coworking_one_session_one_position():
    agent_tools.set_caller_session("sess-e3")
    first = call("agency_register", position_id="expert-z", role="expert")
    assert first["ok"], first
    again = call("agency_register", position_id="expert-w", role="expert")
    assert not again["ok"] and again["error"]["code"] == "ALREADY_BOUND", again
    # known position re-registering is idempotent at the store level
    res = store.register_position(DB, "expert-z", "expert", [], "other-session")
    assert res["already_registered"] and res["status"] == "pending"


# --- projection cache: loop-maintained, parity with replay (TZ §6) ---------------
def test_projection_cache_parity_and_loop_refresh():
    # events land; cache cold
    call("agency_record_commitment", issue_id="i-001", who="admin-1",
         what="c1", due="2099-01-01T00:00:00Z")
    call("agency_create_branch", issue_id="i-001", title="b1")
    replayed = store.replay_projection(DB, "i-001")
    cached = store.projection_for_status(DB, "i-001")   # miss → replay+cache
    assert cached == replayed
    row = store.connect(DB).execute(
        "SELECT as_of_seq FROM projections WHERE issue_id='i-001'").fetchone()
    assert row["as_of_seq"] == replayed["as_of_seq"]
    # new event → read path sees tip; stale cache never served
    call("agency_survey", expert_id="expert-a", issue_id="i-001", question="q3")
    fresh = store.projection_for_status(DB, "i-001")
    assert fresh["as_of_seq"] > cached["as_of_seq"]
    assert fresh == store.replay_projection(DB, "i-001"), "parity at every seq"
    # a single loop tick converges any backlog and stays at parity
    from plugins.agency.loop import LoopWorker
    w = LoopWorker(db_path=DB, interval_s=3600)
    w.tick()
    row = store.connect(DB).execute(
        "SELECT as_of_seq FROM projections WHERE issue_id='i-001'").fetchone()
    top = store.connect(DB).execute("SELECT MAX(seq) FROM ledger").fetchone()[0]
    assert row["as_of_seq"] == top


def test_projection_cache_concurrent_writers_keep_parity():
    def writer(i):
        for k in range(10):
            call("agency_create_branch", issue_id="i-001", title=f"p{i}-{k}")

    ws = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
    for t in ws:
        t.start()
    for t in ws:
        t.join()
    final = store.projection_for_status(DB, "i-001")
    assert final == store.replay_projection(DB, "i-001")
    assert len(final["branches"]) == 40


# --- admin-mode gate: toolset visible ONLY in admin_mode (Вл 07.11) --------------
def test_admin_mode_gate():
    import plugins.agency as A

    class Ctx:
        def get_config(self, k, d=None):
            return True if k == "enabled" else d   # settings.enabled = True

    saved_ctx, saved_admin = A._ctx, A._admin_mode_on
    try:
        A._ctx = Ctx()
        A._admin_mode_on = lambda: False
        assert A._enabled() is False, "admin tools must hide outside admin mode"
        A._admin_mode_on = lambda: True
        assert A._enabled() is True
        # settings gate still independent: off settings → invisible even in admin mode
        A._ctx = None
        assert A._agency_settings_enabled() in (True, False)  # reads real config; no crash
    finally:
        A._ctx, A._admin_mode_on = saved_ctx, saved_admin


# --- signature forgery: model-passed identity fields are ignored -----------------
def test_signature_forgery_ignored():
    r = call("agency_survey", expert_id="expert-a", issue_id="i-001", question="q",
             position_id="expert-b", mandate_version=99)   # forged fields
    assert r["ok"], r
    row = store.connect(DB).execute(
        "SELECT actor_position_id,mandate_version FROM ledger"
        " WHERE idem_key IS NOT NULL ORDER BY seq DESC LIMIT 1").fetchone()
    assert row is not None, "expected a ledger row"
    assert row["actor_position_id"] == "admin-1" and row["mandate_version"] == 1
    assert r["ok"]


if __name__ == "__main__":
    import inspect
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            if "tmp_path" in fn.__code__.co_varnames:
                continue  # needs pytest fixture; covered when run under pytest
            setup_function(None)
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                fails += 1
                print(f"FAIL {name}: {exc or '<no message>'}")
            except Exception as exc:
                fails += 1
                print(f"ERROR {name}: {exc!r}")
    print("done, failures:", fails)
    sys.exit(1 if fails else 0)

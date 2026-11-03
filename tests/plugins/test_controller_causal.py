"""ctl_causal_link — causal DAG edges in state.db (V-O9 «плоско пишем — графом читаем»,
core ТЗ 03.11). Covers: canonical schema presence, writer contract (cycle/self-loop/
dangling/ts-inversion rejection + idempotent absorb), the live shadow writers
(transport reply mirror, todo->task causes, contract/mem supersedes, branch fork/join),
the idempotent migration over legacy pairwise columns, the three readers
(supersede_impact, projection, causal_freshness) and the invariant audit."""
import sqlite3
import time

import pytest

from controller import causal, model, transport
from controller import topic_memory as tm
from hermes_state_common import SCHEMA_SQL


@pytest.fixture()
def conn(tmp_path):
    c = model.connect(tmp_path / "state.db")  # raw SCHEMA_SQL temp DB (controller precedent)
    yield c
    c.close()


def _emit(conn, n):
    # Strictly increasing event stamps, waited as a condition (HX013): re-emit
    # while the clock has not ticked past the previous insert (sub-microsecond
    # on Linux, so the loop exits on the first or second try).
    eids, prev_ts = [], 0.0
    for i in range(n):
        while True:
            eid = model.emit(conn, kind="t", origin="test", payload={"i": i})
            ts = conn.execute("SELECT ts FROM ctl_event WHERE event_id=?",
                              (eid,)).fetchone()[0]
            if ts > prev_ts:
                eids.append(eid)
                prev_ts = ts
                break
    return eids


# ---- schema ------------------------------------------------------------------------

def test_fresh_db_has_causal_table(conn):
    row = conn.execute("SELECT name FROM sqlite_master WHERE type='table'"
                       " AND name='ctl_causal_link'").fetchone()
    assert row, "ctl_causal_link missing from fresh SCHEMA_SQL DB"
    idx = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='ctl_causal_link'")}
    assert "ux_cl_edge" in idx  # idempotency triple (ТЗ v2)


# ---- writer contract ----------------------------------------------------------------

def test_link_happy_and_absorb(conn):
    a, b = _emit(conn, 2)
    assert causal.link(conn, frm=f"ctl:{a}", to=f"ctl:{b}", kind="causes")
    assert causal.link(conn, frm=f"ctl:{a}", to=f"ctl:{b}", kind="causes") is None
    assert conn.execute("SELECT COUNT(*) FROM ctl_causal_link").fetchone()[0] == 1


@pytest.mark.parametrize("frm,to,kind", [
    pytest.param("ctl:{b}", "ctl:{a}", "causes", id="cycle"),      # after a->b
    pytest.param("ctl:{a}", "ctl:{a}", "causes", id="self-loop"),
    pytest.param("ctl:{a}", "ctl:nope", "causes", id="dangling"),
    pytest.param("bogus:x", "ctl:{b}", "causes", id="unknown-ns"),
    pytest.param("ctl:{a}", "ctl:{b}", "wat", id="unknown-kind"),
])
def test_writer_contract_rejects(conn, frm, to, kind):
    a, b = _emit(conn, 2)
    if kind != "wat":
        causal.link(conn, frm=f"ctl:{a}", to=f"ctl:{b}", kind="causes")
    with pytest.raises(causal.CausalContractError):
        causal.link(conn, frm=frm.format(a=a, b=b), to=to.format(a=a, b=b), kind=kind)
    if kind != "wat":
        assert conn.execute("SELECT COUNT(*) FROM ctl_causal_link").fetchone()[0] == 1


def test_writer_contract_rejects_ts_inversion(conn):
    a, b = _emit(conn, 2)  # b is younger than a
    with pytest.raises(causal.CausalContractError):
        causal.link(conn, frm=f"ctl:{b}", to=f"ctl:{a}", kind="causes")


def test_reply_kind_survives_out_of_order_ingestion_ts(conn):
    # parent OBSERVED after its child: node stamps invert, edge must still land
    a, b = _emit(conn, 2)  # b observed later (older event in the room is 'a')
    assert causal.link(conn, frm=f"ctl:{b}", to=f"ctl:{a}", kind="reply")


def test_non_strict_writer_returns_none_never_raises(conn):
    assert causal.link(conn, frm="ctl:ghost", to="ctl:ghost2", kind="causes",
                       strict=False) is None


def test_cycle_contract_transitive(conn):
    a, b, c = _emit(conn, 3)
    causal.link(conn, frm=f"ctl:{a}", to=f"ctl:{b}", kind="causes")
    causal.link(conn, frm=f"ctl:{b}", to=f"ctl:{c}", kind="about")
    with pytest.raises(causal.CausalContractError):
        causal.link(conn, frm=f"ctl:{c}", to=f"ctl:{a}", kind="merge")


# ---- live shadow writers -------------------------------------------------------------

def test_todo_materialize_writes_causes_edge(conn):
    todo = model.todo_add(conn, owner="me", text="do the thing")
    tid = model.todo_materialize(conn, todo_id=todo, workdir="/tmp")
    row = conn.execute("SELECT kind FROM ctl_causal_link WHERE from_event=? AND to_event=?",
                       (f"todo:{todo}", f"task:{tid}")).fetchone()
    assert row and row[0] == "causes"


def test_revise_contract_writes_supersedes_edge(conn):
    tid = model.create_task(conn, owner="me", workdir="/tmp", goal="g")
    v2 = model.revise_contract(conn, tid, {"goal": "g2"}, "MINOR")
    row = conn.execute("SELECT kind FROM ctl_causal_link WHERE from_event=? AND to_event=?",
                       (f"rev:{tid}@1", f"rev:{tid}@{v2}")).fetchone()
    assert row and row[0] == "supersedes"


def test_mem_supersede_writes_edge_and_impact(conn):
    scope = tm.ensure_scope(conn, "task", "t1")
    m1 = tm.add_item(conn, scope, "fact", "subject", "old rule", "user-directive")
    m2 = tm.add_item(conn, scope, "fact", "subject", "new rule", "user-directive")
    m3 = tm.add_item(conn, scope, "fact", "subject", "newest rule", "user-directive")
    impact = causal.supersede_impact(conn, f"mem:{m1}")
    assert [d["node"] for d in impact] == [f"mem:{m2}", f"mem:{m3}"]


def test_confirm_writes_supersedes_edges(conn):
    scope = tm.ensure_scope(conn, "task", "t1")
    m1 = tm.add_item(conn, scope, "fact", "subj", "rule", "user-directive")
    m2 = tm.add_item(conn, scope, "fact", "subj", "proposal", "agent-proposal")
    assert tm.confirm(conn, m2)
    assert causal.supersede_impact(conn, f"mem:{m1}")[0]["node"] == f"mem:{m2}"


def test_transport_reply_mirror(conn):
    parent = transport.record_observation(conn, connector="matrix", channel="sync",
                                          room_id="!r", event_id="$p", relation={})
    child = transport.record_observation(conn, connector="matrix", channel="sync",
                                         room_id="!r", event_id="$c",
                                         relation={"m.in_reply_to": {"event_id": "$p"}})
    row = conn.execute("SELECT 1 FROM ctl_causal_link WHERE from_event=? AND to_event=?"
                       " AND kind='reply'",
                       (f"obs:{parent['event_obs_id']}", f"obs:{child['event_obs_id']}"))
    assert row.fetchone()


def test_transport_reply_out_of_order_is_no_edge(conn):
    # child lands before its parent: no edge now (parent absent), migrate picks it up later
    transport.record_observation(conn, connector="matrix", channel="sync",
                                 room_id="!r", event_id="$c",
                                 relation={"m.in_reply_to": {"event_id": "$p"}})
    assert conn.execute("SELECT COUNT(*) FROM ctl_causal_link").fetchone()[0] == 0
    transport.record_observation(conn, connector="matrix", channel="sync",
                                 room_id="!r", event_id="$p", relation={})
    stats = causal.migrate(conn)
    assert stats["reply_new"] == 1


# ---- fork -> merge DAG -----------------------------------------------------------------

def _branch_fixture(conn, now):
    conn.execute("INSERT INTO sessions (id, source, started_at) VALUES ('sp','cli',?)",
                 (now - 100,))
    conn.execute("INSERT INTO sessions (id, source, started_at) VALUES ('sc','cli',?)",
                 (now - 50,))
    conn.execute("INSERT INTO session_branches (branch_id,parent_session_id,"
                 "child_session_id,created_at,status) VALUES ('b1','sp','sc',?,'active')",
                 (now - 40,))


def test_fork_migrate_then_join_gives_correct_dag(conn):
    now = time.time()
    _branch_fixture(conn, now)
    stats = causal.migrate(conn)
    assert stats["causes_new"] == 1 and stats["merge_new"] == 0
    edges = {(r[0], r[1], r[2]) for r in conn.execute(
        "SELECT from_event,to_event,kind FROM ctl_causal_link")}
    assert ("sess:sp", "br:b1", "causes") in edges
    # join: convergence event keeps it a DAG (a raw br->sess:sp edge would cycle)
    eid = causal.record_branch_join(conn, branch_id="b1", parent_session_id="sp")
    assert eid
    dag = causal.descendants(conn, "sess:sp")
    by = {d["node"]: d for d in dag}
    assert by["br:b1"]["depth"] == 1
    assert f"ctl:{eid}" in by  # min-depth dedup: also reachable depth-1 via sess->evt
    # the join point sits DOWNSTREAM of the branch itself:
    order = [p["node"] for p in causal.projection(conn, ["sess:sp"])]
    assert order.index("br:b1") < order.index(f"ctl:{eid}")
    assert causal.check_invariants(conn) == {}
    # replay: join already recorded
    assert causal.record_branch_join(conn, branch_id="b1", parent_session_id="sp") is None


# ---- migration shapes & idempotency ------------------------------------------------------

def test_migrate_is_idempotent_and_invariants_green(conn):
    now = time.time()
    _branch_fixture(conn, now)
    conn.execute("UPDATE session_branches SET status='joined', joined_at=? WHERE branch_id='b1'",
                 (now - 10,))
    conn.execute("INSERT INTO ctl_mem_scope VALUES ('s','global','',?)", (now - 90,))
    conn.execute("INSERT INTO ctl_mem_item (item_id,scope_id,kind,subject,body,source,"
                 "status,superseded_by,confirmed,created_at) VALUES ('m1','s','fact','x','a',"
                 "'user-directive','superseded','m2',1,?)", (now - 80,))
    conn.execute("INSERT INTO ctl_mem_item (item_id,scope_id,kind,subject,body,source,"
                 "status,confirmed,created_at) VALUES ('m2','s','fact','x','b',"
                 "'user-directive','active',1,?)", (now - 70,))
    st1 = causal.migrate(conn)
    assert not st1["skipped"], st1["skipped"]
    total1 = conn.execute("SELECT COUNT(*) FROM ctl_causal_link").fetchone()[0]
    st2 = causal.migrate(conn)
    assert (st2["reply_new"] + st2["causes_new"] + st2["merge_new"]
            + st2["supersedes_new"]) == 0
    assert conn.execute("SELECT COUNT(*) FROM ctl_causal_link").fetchone()[0] == total1
    assert causal.check_invariants(conn) == {}
    assert causal.supersede_impact(conn, "mem:m1")[0]["node"] == "mem:m2"


def test_late_parent_reply_edge_lands_on_migrate(conn):
    # child lands first (no edge yet); parent arrives stamped with an EARLIER
    # observation time: the reply edge still lands on the migration sweep
    transport.record_observation(conn, connector="matrix", channel="sync",
                                 room_id="!r", event_id="$c",
                                 relation={"m.in_reply_to": {"event_id": "$p"}})
    # parent row with an EARLIER created_at, inserted after: inversion at edge time
    conn.execute("INSERT INTO event_observations (event_obs_id,observation_id,room_id,"
                 "event_id,created_at) VALUES ('evobs-old','tobs-1','!r','$p',?)",
                 (time.time() - 5000,))
    conn.commit()
    stats = causal.migrate(conn)
    assert stats["reply_new"] == 1  # child created now, parent stamped older -> ok


# ---- readers -------------------------------------------------------------------------------

def test_projection_is_topological_on_diamond(conn):
    a, b, c, d = _emit(conn, 4)
    causal.link(conn, frm=f"ctl:{a}", to=f"ctl:{b}", kind="causes")
    causal.link(conn, frm=f"ctl:{a}", to=f"ctl:{c}", kind="causes")
    causal.link(conn, frm=f"ctl:{b}", to=f"ctl:{d}", kind="merge")
    causal.link(conn, frm=f"ctl:{c}", to=f"ctl:{d}", kind="merge")
    order = [p["node"] for p in causal.projection(conn, [f"ctl:{a}"])]
    assert order.index(f"ctl:{d}") > order.index(f"ctl:{b}")
    assert order.index(f"ctl:{d}") > order.index(f"ctl:{c}")
    assert order[0] == f"ctl:{a}"


def test_freshness_is_longest_chain_not_levels(conn):
    a, b, c = _emit(conn, 3)
    causal.link(conn, frm=f"ctl:{a}", to=f"ctl:{b}", kind="causes")
    causal.link(conn, frm=f"ctl:{b}", to=f"ctl:{c}", kind="causes")
    causal.link(conn, frm=f"ctl:{a}", to=f"ctl:{c}", kind="reply")  # shortcut to depth-1
    assert causal.causal_freshness(conn, f"ctl:{a}") == 2  # a->b->c chain wins
    assert causal.causal_freshness(conn, f"ctl:{c}") == 0


# ---- crash seams (stage-d) ------------------------------------------------------------------

def test_join_crash_after_event_commit_finishes_edges_on_replay(conn):
    """The worst seam: ctl_event for a join committed, its merge/causes edges lost.
    Replay must find the event by idempotency_key and finish the shape, not emit a
    second convergence node."""
    import json as _json
    now = time.time()
    _branch_fixture(conn, now)
    conn.execute("UPDATE session_branches SET status='joined' WHERE branch_id='b1'")
    stranded = model.emit(conn, kind="branch_joined", origin="causal",
                          payload={"branch_id": "b1", "parent_session_id": "sp",
                                   "migration": True},
                          idempotency_key="branch_join:b1")
    eid = causal.record_branch_join(conn, branch_id="b1", parent_session_id="sp",
                                    migration=True)
    assert eid == stranded
    kinds = {r[0] for r in conn.execute(
        "SELECT kind FROM ctl_causal_link WHERE to_event=?", (f"ctl:{eid}",))}
    assert kinds == {"merge", "causes"}
    assert conn.execute("SELECT COUNT(*) FROM ctl_event WHERE kind='branch_joined'"
                        ).fetchone()[0] == 1


def test_sigkill_mid_migration_leaves_no_partial_edges(tmp_path):
    """Chaos: SIGKILL a child mid-migration repeatedly; the survivor DB must always
    be contract-clean and the finished migration must equal the uninterrupted one."""
    import os
    import signal
    import sqlite3 as s3
    db = str(tmp_path / "chaos.db")
    seed = s3.connect(db)
    seed.executescript(SCHEMA_SQL)
    now = time.time()
    for i in range(120):
        seed.execute("INSERT INTO event_observations (event_obs_id,observation_id,"
                     "room_id,event_id,relation,created_at) VALUES (?,?,?,?,?,?)",
                     (f"evobs-{i}", f"tobs-{i}", "!r", f"$e{i}",
                      f'{{"m.in_reply_to": {{"event_id": "$e{max(i-1, 0)}"}}}}'
                      if i else "{}", now - 5000 + i))
    seed.commit(); seed.close()

    child_src = (
        "import sqlite3, sys; sys.path.insert(0, {!r})\n"
        "from controller import causal\n"
        "conn = sqlite3.connect({!r}); conn.row_factory = sqlite3.Row\n"
        "causal.migrate(conn)\n"
    ).format("/home/qwen/hermes-agent", db)
    for _ in range(3):  # three kill windows across the append-only sweep
        p = os.spawnlp(os.P_NOWAIT, __import__("sys").executable,
                       "python", "-c", child_src)
        try:
            os.kill(p, signal.SIGKILL)
        except ProcessLookupError:
            pass  # finished before the kill landed — fine
        os.waitpid(p, 0)
        check = s3.connect(db)
        assert causal.check_invariants(check) == {}
        check.close()
    import subprocess as sp
    sp.run([__import__("sys").executable, "-c", child_src], check=True)
    final = s3.connect(db)
    n = final.execute("SELECT COUNT(*) FROM ctl_causal_link WHERE kind='reply'"
                      ).fetchone()[0]
    assert n == 119  # every parent-child pair landed exactly once
    assert causal.check_invariants(final) == {}
    final.close()




# ---- v2 legacy sweep ---------------------------------------------------------------

def test_legacy_v1_schema_upgraded_in_place(tmp_path):
    """DB built before ТЗ v2 (old unique-index name, AUTOINCREMENT table):
    ensure_schema must land the v2 shape without losing rows or the
    idempotency guarantee."""
    c = model.connect(tmp_path / "legacy.db")
    a, b = _emit(c, 2)
    # downgrade the causal table to its pre-v2 shape, exactly as found in the
    # live state.db (AUTOINCREMENT residue + triple under the old index name)
    c.execute("DROP INDEX ux_cl_edge")
    c.execute("DROP TABLE ctl_causal_link")
    c.execute("""CREATE TABLE ctl_causal_link (
        link_id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
        from_event TEXT NOT NULL, to_event TEXT NOT NULL, kind TEXT NOT NULL,
        payload TEXT)""")
    c.execute("CREATE UNIQUE INDEX udl_ctl_causal_link"
              " ON ctl_causal_link(from_event, to_event, kind)")
    c.execute("INSERT INTO ctl_causal_link(ts,from_event,to_event,kind)"
              " VALUES (1.0,?,?,'causes')", (f"ctl:{a}", f"ctl:{b}"))
    c.commit()

    causal.ensure_schema(c)  # the sweep runs on every entry point
    idx = {r[0] for r in c.execute("SELECT name FROM sqlite_master"
                                   " WHERE type='index' AND tbl_name='ctl_causal_link'"
                                   " AND name IS NOT NULL")}
    assert "ux_cl_edge" in idx and "udl_ctl_causal_link" not in idx
    assert c.execute("SELECT COUNT(*) FROM ctl_causal_link").fetchone()[0] == 1
    # idempotency key survived the rename: a replayed edge is absorbed
    assert causal.link(c, frm=f"ctl:{a}", to=f"ctl:{b}", kind="causes") is None
    assert causal.check_invariants(c) == {}
    c.close()

"""Investigation self-check mechanism: DB layer + tool contract tests.

Deterministic, tmp-DB only (no gateway, no model). Pins the mechanism
invariants from docs/design/investigation-mechanism.md:

1. The DDL boots: a fresh SessionDB has the investigation tables.
2. open is idempotent per session (re-delivery never forks a second open row).
3. Predictions must be recorded BEFORE the probe's observation arrives.
4. Duplicate delivery of the same tool-call / same branch join is deduped
   (UNIQUE observations.dedup_key) — one observation, no double evidence.
5. An observation-candidate becomes accounted ONLY through assess.
6. close without assess leaves the investigation OPEN with a diagnostic.
7. A dispatched probe whose result never crossed the restart is settled to
   unknown_outcome, never re-run implicitly.
8. The ``investigation`` tool exposes exactly the four model-facing operations.
"""

from __future__ import annotations

import json

import pytest

from hermes_state import SessionDB


@pytest.fixture()
def db(tmp_path):
    instance = SessionDB(db_path=tmp_path / "state.db")
    instance.ensure_session("sess-parent", source="test", session_key="k-parent")
    yield instance
    instance.close()


@pytest.fixture()
def inv(db: SessionDB):
    iid, _created = db.open_investigation(db.new_investigation_id(), "sess-parent",
                                         "why did the cache go stale?")
    h1 = db.add_hypothesis(iid, "H1: eviction skipped the key")
    h2 = db.add_hypothesis(iid, "H2: writer overwrote fresh data")
    p1 = db.add_probe(iid, "dump the cache entry")
    db.add_prediction(p1, h1, "entry absent from dump")
    db.add_prediction(p1, h2, "entry present with new value")
    return {"iid": iid, "h1": h1, "h2": h2, "p1": p1}


def _call_key(db: SessionDB, call_id: str) -> str:
    return db.observation_dedup_key_call("sess-parent", call_id)


class TestSchemaBoots:
    def test_ddl_creates_all_tables(self, db: SessionDB):
        names = {r["name"] for r in db._read_all(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"investigations", "investigation_hypotheses", "investigation_probes",
                "investigation_predictions", "investigation_observations"} <= names

    def test_mixin_reachable_from_session_db(self, db: SessionDB):
        # Mixed into SessionDB (hermes_state.py class bases), async facade included.
        from hermes_state import AsyncSessionDB
        assert callable(db.render_active_investigation)
        assert callable(AsyncSessionDB(db).render_active_investigation)


class TestOpen:
    def test_open_lists_in_render(self, db: SessionDB, inv):
        rendered = db.render_active_investigation("sess-parent")
        assert inv["iid"] in rendered and "why did the cache go stale?" in rendered
        assert inv["h1"] in rendered and inv["p1"] in rendered
        assert "entry absent from dump" in rendered  # predictions ride the render

    def test_open_idempotent_per_session(self, db: SessionDB, inv):
        # Re-delivered ``open`` (restart / replay) returns the live row, no fork.
        iid2, created = db.open_investigation(db.new_investigation_id(), "sess-parent", "replay?")
        assert (iid2, created) == (inv["iid"], False)
        assert len(db._read_all("SELECT 1 FROM investigations")) == 1

    def test_closed_session_can_open_again(self, db: SessionDB, inv):
        db.close_investigation(inv["iid"], "done")
        iid2, created = db.open_investigation(db.new_investigation_id(), "sess-parent", "next?")
        assert created and iid2 != inv["iid"]


class TestPredictBeforeObservation:
    def test_prediction_after_observation_not_created(self, db: SessionDB, inv):
        db.capture_observation(inv["iid"], dedup_key=_call_key(db, "c1"), source="tool",
                               content="entry absent", probe_id=inv["p1"])
        _, created = db.add_prediction(inv["p1"], inv["h2"], "written after the fact")
        assert created is False
        assert len(db.list_predictions(inv["p1"])) == 2

    def test_prediction_pair_is_idempotent(self, db: SessionDB, inv):
        _, first = db.add_prediction(inv["p1"], inv["h1"], "entry absent from dump")
        assert first is False  # same (probe, hypothesis) pair already predicted
        assert len(db.list_predictions(inv["p1"])) == 2

    def test_capture_without_predictions_flagged(self, db: SessionDB, inv):
        p2 = db.add_probe(inv["iid"], "unpredicted check")
        obs = db.capture_observation(inv["iid"], dedup_key=_call_key(db, "c2"), source="tool",
                                     content="whatever", probe_id=p2)
        assert json.loads(db.get_observation(obs)["meta_json"])["no_predictions"] is True


class TestDeliveryDedup:
    def test_duplicate_tool_call_delivery_captures_once(self, db: SessionDB, inv):
        first = db.capture_observation(inv["iid"], dedup_key=_call_key(db, "c1"), source="tool",
                                       content="entry absent", probe_id=inv["p1"])
        replay = db.capture_observation(inv["iid"], dedup_key=_call_key(db, "c1"), source="tool",
                                        content="entry absent", probe_id=inv["p1"])
        assert first and replay is None
        assert len(db.list_observations(inv["iid"])) == 1
        assert db.get_observation(first)["probe_id"] == inv["p1"]

    def test_duplicate_branch_join_captures_once(self, db: SessionDB, inv):
        key = db.observation_dedup_key_branch("sess-parent", "bdeadbeef")
        first = db.capture_observation(inv["iid"], dedup_key=key, source="branch",
                                       content="[work-note] ...")
        assert first and db.capture_observation(inv["iid"], dedup_key=key, source="branch",
                                                content="[work-note] ...") is None

    def test_same_call_id_in_other_session_is_not_deduped(self, db: SessionDB, inv):
        db.ensure_session("sess-other", source="test", session_key="k-other")
        other, _ = db.open_investigation(db.new_investigation_id(), "sess-other", "other Q")
        ours = db.capture_observation(inv["iid"], dedup_key=_call_key(db, "c1"),
                                      source="tool", content="a")
        theirs = db.capture_observation(other, source="tool", content="b",
                                        dedup_key=db.observation_dedup_key_call("sess-other", "c1"))
        assert ours and theirs  # key is session-scoped; provider call_ids may collide across sessions

    def test_probe_marked_observed_on_capture(self, db: SessionDB, inv):
        db.capture_observation(inv["iid"], dedup_key=_call_key(db, "c1"), source="tool",
                               content="entry absent", probe_id=inv["p1"])
        assert db.get_probe(inv["p1"])["status"] == "observed"


class TestAssessAccounting:
    def test_candidate_accounted_only_through_assess(self, db: SessionDB, inv):
        obs = db.capture_observation(inv["iid"], dedup_key=_call_key(db, "c1"), source="tool",
                                     content="entry absent", probe_id=inv["p1"])
        assert db.unassessed_count(inv["iid"]) == 1
        assert db.assess_observation(obs, "matches H1's expectation, contradicts H2",
                                     [{"hypothesis_id": inv["h1"], "status": "leading"},
                                      {"hypothesis_id": inv["h2"], "status": "refuted"}])
        assert db.unassessed_count(inv["iid"]) == 0
        stored = db.get_observation(obs)
        assert stored["assessed"] == 1 and "matches H1" in stored["verdict"]
        assert {h["hypothesis_id"]: h["status"] for h in db.list_hypotheses(inv["iid"])} \
            == {inv["h1"]: "leading", inv["h2"]: "refuted"}

    def test_assess_bumps_revision(self, db: SessionDB, inv):
        before = db.get_investigation(inv["iid"])["revision"]
        obs = db.capture_observation(inv["iid"], dedup_key=_call_key(db, "c1"), source="tool",
                                     content="x", probe_id=inv["p1"])
        db.assess_observation(obs, "noted")
        assert db.get_investigation(inv["iid"])["revision"] > before


class TestForkBinding:
    def test_branch_meta_binding_round_trip(self, db: SessionDB, inv):
        # /fork hook writes provenance into session_branches.meta_json; the join
        # hook reads it back to route the result to the right probe.
        db.ensure_session("sess-parent-f1", source="test", session_key="k-f1")
        db.create_branch_row("b1a2c3d4", "sess-parent", "sess-parent-f1", "uid-1", title="probe run")
        assert db.bind_probe_branch(inv["p1"], "b1a2c3d4")
        assert db.set_branch_meta("b1a2c3d4", {"investigation_id": inv["iid"],
                                              "probe_id": inv["p1"], "purpose": "dump cache"})
        meta = json.loads(db.get_branch("b1a2c3d4")["meta_json"])
        assert meta["title"] == "probe run"  # merge, not clobber
        assert meta["investigation_id"] == inv["iid"] and meta["probe_id"] == inv["p1"]
        assert db.set_branch_meta("nope", {}) is False

class TestCloseDiscipline:
    def test_close_without_assess_stays_open_with_diagnostic(self, db: SessionDB, inv):
        db.capture_observation(inv["iid"], dedup_key=_call_key(db, "c1"), source="tool",
                               content="entry absent", probe_id=inv["p1"])
        result = db.close_investigation(inv["iid"], "conclusion", "one host only")
        assert result == {"closed": False, "unassessed": 1}
        row = db.get_investigation(inv["iid"])
        assert row["status"] == "open"
        assert row["closed_at"] is None
        diag = db.close_diagnostic(inv["iid"])
        assert diag["unassessed"] == 1 and diag["pending_summary"] == "conclusion"
        # The turn may still end and answer the human; state says why it stayed open.
        assert "close pending" in db.render_active_investigation("sess-parent")

    def test_close_after_assess_closes(self, db: SessionDB, inv):
        obs = db.capture_observation(inv["iid"], dedup_key=_call_key(db, "c1"), source="tool",
                                     content="entry absent", probe_id=inv["p1"])
        db.assess_observation(obs, "H1 leading")
        result = db.close_investigation(inv["iid"], "stale eviction", "observed on one node")
        assert result["closed"] is True
        row = db.get_investigation(inv["iid"])
        assert row["status"] == "closed" and row["summary"] == "stale eviction"
        assert row["constraints"] == "observed on one node"
        assert db.render_active_investigation("sess-parent") is None

    def test_edits_on_closed_investigation_are_refused(self, db: SessionDB, inv):
        db.close_investigation(inv["iid"], "done")
        assert db.add_hypothesis(inv["iid"], "too late") is None
        assert db.add_probe(inv["iid"], "too late") is None


class TestRestartSettle:
    def test_dispatched_probe_without_result_settled_unknown(self, db: SessionDB, inv):
        lost = db.add_probe(inv["iid"], "/fork a branch that dies mid-run")
        landed = db.add_probe(inv["iid"], "tool call that landed before the crash")
        db.set_probe_status(lost, "dispatched")
        db.set_probe_status(landed, "dispatched")
        db.capture_observation(inv["iid"], dedup_key=_call_key(db, "c9"), source="tool",
                               content="landed", probe_id=landed)
        assert db.settle_unresolved_probes() == 1
        assert db.get_probe(lost)["status"] == "unknown_outcome"  # source unknown: never re-run
        assert db.get_probe(landed)["status"] == "observed"
        assert db.get_investigation(inv["iid"])["status"] == "open"  # stays open; boot wakes owner

    def test_settle_scoped_to_session(self, db: SessionDB, inv):
        db.ensure_session("sess-other", source="test", session_key="k2")
        other_iid, _ = db.open_investigation(db.new_investigation_id(), "sess-other", "q2")
        other_probe = db.add_probe(other_iid, "other")
        db.set_probe_status(other_probe, "dispatched")
        assert db.settle_unresolved_probes("sess-parent") == 0
        assert db.get_probe(other_probe)["status"] == "dispatched"
        assert db.settle_unresolved_probes("sess-other") == 1


class TestToolContract:
    def _tool(self):
        import tools.investigation_tool as m
        return m

    def test_registered_in_sessions_toolset(self):
        from tools.registry import registry
        import tools.investigation_tool  # self-registration on import
        assert registry.get_toolset_for_tool("investigation") == "sessions"
        assert registry.get_schema("investigation")["name"] == "investigation"

    def test_four_operations_and_dispatch_errors(self, db: SessionDB):
        m = self._tool()
        assert set(m._OPS) == {"open", "predict", "assess", "close"}
        bad = json.loads(m.investigation({"op": "nope"}, db=db, current_session_id="sess-parent"))
        assert bad["ok"] is False and set(bad["ops"]) == {"open", "predict", "assess", "close"}
        nodb = json.loads(m.investigation({"op": "open"}, db=None, current_session_id=""))
        assert nodb["ok"] is False

    def test_tool_open_predict_capture_assess_close(self, db: SessionDB):
        m = self._tool()
        opened = json.loads(m.investigation(
            {"op": "open", "question": "does the retry storm start at the limiter?",
             "hypotheses": ["limiter bursts", "upstream flaps"],
             "probes": [{"description": "count limiter rejections",
                         "predictions": [{"hypothesis": 0, "expected": "many rejections"},
                                         {"hypothesis": 1, "expected": "few rejections"}]}]},
            db=db, current_session_id="sess-parent"))
        assert opened["ok"] and len(opened["hypothesis_ids"]) == 2
        iid = opened["investigation_id"]
        probe = opened["probes"][0]["probe_id"]
        assert all(p["created"] for p in opened["probes"][0]["predictions"])
        # runtime hook attaches the observation with its call_id
        obs = db.capture_observation(iid, dedup_key=_call_key(db, "tc1"), source="tool",
                                     content="1247 rejections", probe_id=probe)
        # model assesses through the tool
        assessed = json.loads(m.investigation(
            {"op": "assess", "observation_id": obs, "verdict": "limiter bursts: many rejections",
             "hypothesis_updates": [{"hypothesis_id": opened["hypothesis_ids"][0],
                                     "status": "leading"}]},
            db=db, current_session_id="sess-parent"))
        assert assessed["ok"] and assessed["unassessed_remaining"] == 0
        closed = json.loads(m.investigation(
            {"op": "close", "summary": "limiter bursts", "constraints": "one window only"},
            db=db, current_session_id="sess-parent"))
        assert closed["ok"] and closed["closed"] is True
        assert db.get_open_investigation("sess-parent") is None

    def test_tool_close_refused_while_unassessed(self, db: SessionDB):
        m = self._tool()
        opened = json.loads(m.investigation(
            {"op": "open", "question": "q", "hypotheses": ["a"]},
            db=db, current_session_id="sess-parent"))
        db.capture_observation(opened["investigation_id"], dedup_key=_call_key(db, "tc2"),
                               source="tool", content="x")
        refused = json.loads(m.investigation({"op": "close", "summary": "premature"},
                                             db=db, current_session_id="sess-parent"))
        assert refused["ok"] and refused["closed"] is False and refused["unassessed"] == 1
        assert db.get_investigation(opened["investigation_id"])["status"] == "open"

    def test_tool_predict_after_observation_reports_not_created(self, db: SessionDB):
        m = self._tool()
        opened = json.loads(m.investigation(
            {"op": "open", "question": "q", "hypotheses": ["a"],
             "probes": [{"description": "p"}]},
            db=db, current_session_id="sess-parent"))
        probe = opened["probes"][0]["probe_id"]
        db.capture_observation(opened["investigation_id"], dedup_key=_call_key(db, "tc3"),
                               source="tool", content="x", probe_id=probe)
        hypo = opened["hypothesis_ids"][0]
        late = json.loads(m.investigation(
            {"op": "predict", "probe_id": probe,
             "predictions": [{"hypothesis_id": hypo, "expected": "after the fact"}]},
            db=db, current_session_id="sess-parent"))
        assert late["ok"] and late["predictions"][0]["created"] is False and late["note"]

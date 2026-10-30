"""Name-first fork UI (Вл 30.10: "писать /join с хексами — издевательство").

Hex ids stay as fallback identity; every human-facing surface resolves and
reports branch NAMES (immutable meta_json title written at fork time).
"""
from __future__ import annotations

import json

import pytest

from gateway.branch_prefix import branch_display_name
from hermes_state import SessionDB
from tools.session_fork_tool import session_fork, session_join


@pytest.fixture()
def db(tmp_path):
    instance = SessionDB(db_path=tmp_path / "state.db")
    yield instance
    instance.close()


def _seed(db):
    db.ensure_session("sess-parent", source="test", session_key="k-parent")
    db.append_messages_batch("sess-parent", [dict(role="user", content="seed")])
    return "sess-parent"


def test_display_name_prefers_immutable_meta(db):
    _seed(db)
    db.ensure_session("child", source="test", session_key="k@b", parent_session_id="sess-parent")
    bid = db.new_branch_id()
    db.create_branch_row(bid, "sess-parent", "child", "uid1", title="test2")
    row = db.get_branch(bid)
    assert branch_display_name(row) == "test2"
    # unnamed branch falls back to hex id
    bid2 = db.new_branch_id()
    db.ensure_session("child2", source="test", session_key="k@b2", parent_session_id="sess-parent")
    db.create_branch_row(bid2, "sess-parent", "child2", "uid1")
    assert branch_display_name(db.get_branch(bid2)) == bid2


def test_session_fork_stores_immutable_name(db):
    parent = _seed(db)
    out = json.loads(session_fork(name="обсуждение", db=db, current_session_id=parent))
    assert out["ok"] and out["name"] == "обсуждение"
    row = db.get_branch(out["branch_id"])
    assert json.loads(row["meta_json"])["title"] == "обсуждение"


def test_session_join_by_name(db):
    parent = _seed(db)
    out = json.loads(session_fork(name="audit", db=db, current_session_id=parent))
    # join by NAME, case-insensitive — the hex id is never required
    res = json.loads(session_join(branch_id="Audit", strategy="discard",
                                  db=db, current_session_id=parent))
    assert res["ok"] and res["branch_id"] == out["branch_id"]
    assert db.get_branch(out["branch_id"])["status"] == "joined"


def test_session_join_name_scoped_to_session(db):
    """A name belonging to ANOTHER session's branch must not resolve here."""
    parent = _seed(db)
    other = "sess-other"
    db.ensure_session(other, source="test", session_key="k-other")
    db.append_messages_batch(other, [dict(role="user", content="s")])
    session_fork(name="shared-name", db=db, current_session_id=other)
    res = json.loads(session_join(branch_id="shared-name", strategy="discard",
                                  db=db, current_session_id=parent))
    assert not res["ok"]


def test_join_hex_still_works(db):
    parent = _seed(db)
    out = json.loads(session_fork(name="keep-hex", db=db, current_session_id=parent))
    bid = out["branch_id"]
    res = json.loads(session_join(branch_id=bid, strategy="discard",
                                  db=db, current_session_id=parent))
    assert res["ok"]

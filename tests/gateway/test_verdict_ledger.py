"""gateway/verdict_ledger: agent success grades as revocable rows, not prose.

The mechanism under test is the transition table itself — propose is agent-voiced
and never self-confirming; confirm only moves 'proposed'; overturn is legal from
ANY live state and demands a reason (that is what makes a wrong grade fixable at
all); the injection note tells the model which of its claims are unconfirmed and
which were overturned."""
import json
import sqlite3

import pytest

from gateway.verdict_ledger import (confirm_c, live_verdicts_c, overturn_c,
                                    propose_c, render_note_c,
                                    overturned_recently_c)


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "state.db"))
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def _p(conn, subject="шаг 1", verdict="сделано"):
    return propose_c(conn, "s1", subject, verdict, json.dumps({"tool_call_uids": ["u1"]}))


def test_propose_is_pinned_agent_issuer_and_starts_proposed(conn):
    vid = _p(conn)
    row = dict(conn.execute("SELECT * FROM verdicts WHERE id=?", (vid,)).fetchone())
    assert row["issuer"] == "agent" and row["state"] == "proposed"


def test_confirm_only_from_proposed_and_names_the_verifier(conn):
    vid = _p(conn)
    out = confirm_c(conn, vid, by="critic-8012", proof="repro: eval score 0.61")
    assert out is not None
    assert out["state"] == "confirmed" and out["by_who"] == "critic-8012"
    with pytest.raises(ValueError):
        confirm_c(conn, vid, by="critic-8012")  # confirmed cannot re-confirm


def test_overturn_is_legal_from_any_live_state_and_requires_reason(conn):
    a = _p(conn, "a")
    b = _p(conn, "b")
    confirm_c(conn, b, by="critic")
    assert overturn_c(conn, a, by="vovak", reason="неверная трактовка «верни»")["state"] == "overturned"
    assert overturn_c(conn, b, by="vovak", reason="критик тоже ошибся")["state"] == "overturned"
    with pytest.raises(ValueError):
        overturn_c(conn, a, by="vovak", reason=" ")  # already overturned
    with pytest.raises(ValueError):
        overturn_c(conn, b, by="vovak", reason="")  # a reason is the record


def test_live_is_latest_per_subject_overturn_exits_live(conn):
    _p(conn, "x", "первая оценка")
    second = _p(conn, "x", "вторая оценка")
    y = _p(conn, "y")
    live = {(v["subject"], v["id"]) for v in live_verdicts_c(conn, "s1")}
    assert ("x", second) in live and ("y", y) in live
    assert sum(1 for s, _ in live if s == "x") == 1
    overturn_c(conn, second, by="vl", reason="r")
    assert all(v["subject"] != "x" for v in live_verdicts_c(conn, "s1"))
    assert [v["id"] for v in overturned_recently_c(conn, "s1")] == [second]


def test_render_note_semantics(conn):
    assert render_note_c(conn, "s1") is None  # empty register costs nothing
    a = _p(conn, "откат gpu.sh")
    b = _p(conn, "датасет собран")
    confirm_c(conn, b, by="eval")
    overturn_c(conn, _p(conn, "мёртвый шаг"), by="vovak", reason="самовольно")
    note = render_note_c(conn, "s1")
    assert note is not None
    assert "<verdict-register>" in note
    assert f"#{a}" in note and "SELF-ASSESSED, NOT CONFIRMED" in note
    assert f"#{b}" in note and "CONFIRMED by 'eval'" in note
    assert "OVERTURNED" in note and "самовольно" in note
    assert "do not restate or silently re-propose" in note


def test_store_wrapper_roundtrip(tmp_path):
    class Store:
        db_path = str(tmp_path / "state.db")

        class _Ctx:
            def __enter__(_):
                _.c = sqlite3.connect(tmp_path / "state.db")
                _.c.row_factory = sqlite3.Row
                return _.c

            def __exit__(_, *a):
                _.c.close()

        def _read_ctx(self):
            return self._Ctx()

    from gateway.verdict_ledger import propose, render_note
    vid = propose(Store(), "s2", "заказ", "готово")
    assert vid > 0
    note = render_note(Store(), "s2")
    assert note is not None
    assert "SELF-ASSESSED" in note and "заказ" in note

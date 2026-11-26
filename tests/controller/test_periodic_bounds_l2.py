"""L2 (TZ-memdoor-5090-v2): per-task bounds on agent periodics — flag-off
refusal of new args, times cap, horizon clamp, overlap refuse."""
import time

import pytest

from controller import periodic_bounds as pb


@pytest.fixture()
def conn(tmp_path):
    import sqlite3
    from controller import followups
    c = sqlite3.connect(tmp_path / "t.db")
    c.row_factory = sqlite3.Row
    followups.ensure_schema(c)
    return c


@pytest.fixture()
def on(monkeypatch):
    monkeypatch.setattr(pb, "agent_periodic_bounds_enabled", lambda: True)


def _add(conn, **kw):
    from controller import followups
    return followups.add(conn, created_by="agent-tool", **kw)


def test_flag_off_refuses_new_args(conn, monkeypatch):
    from plugins.controller import agent_tools
    monkeypatch.setattr(pb, "agent_periodic_bounds_enabled", lambda: False)
    with pytest.raises(ValueError, match="agent_periodic_bounds"):
        agent_tools._periodic_kwargs(conn, {"times": 3}, "reminder", "t", "x")


def test_times_cap_and_horizon_clamp(conn, on):
    v = pb.validate_add(conn, kind="review", scope_task="t", text="a",
                        due_at=None, period_s=3600, rrule=None,
                        now=time.time(), times=100)
    assert "error" in v  # >60
    v = pb.validate_add(conn, kind="review", scope_task="t", text="b",
                        due_at=None, period_s=3600, rrule=None,
                        now=time.time(), times=20, expiry_h=5)
    assert v["max_repeats"] == 5  # 5h window / 1h period


def test_reminder_beyond_expiry_refused(conn, on):
    now = time.time()
    assert "error" in pb.validate_add(conn, kind="reminder", scope_task="t",
                                      text="late", due_at=now + 13 * 3600,
                                      period_s=None, rrule=None, now=now)
    ok = pb.validate_add(conn, kind="reminder", scope_task="t", text="soon",
                         due_at=now + 3600, period_s=None, rrule=None, now=now)
    assert ok == {"max_repeats": None}


def test_overlap_refused_with_existing_id(conn, on):
    fid = _add(conn, kind="reminder", scope_task="t1", text="check ci",
               due_at=time.time() + 600)
    v = pb.validate_add(conn, kind="reminder", scope_task="t1", text="check ci",
                        due_at=time.time() + 900, period_s=None, rrule=None,
                        now=time.time())
    assert fid in v["error"]
    # different scope passes
    assert "error" not in pb.validate_add(conn, kind="reminder", scope_task="t2",
                                          text="check ci", due_at=time.time() + 900,
                                          period_s=None, rrule=None, now=time.time())


def test_tool_path_applies_bounds_end_to_end(conn, on, monkeypatch):
    from plugins.controller import agent_tools
    monkeypatch.setattr(agent_tools, "_conn", lambda: conn)
    out = agent_tools._ctl_followup({"op": "add", "kind": "review",
                                     "text": "sweep logs", "scope_task": "tk",
                                     "period_s": 600, "times": 100})
    assert '"ok": false' in out or "error" in out  # times>60 refused
    out = agent_tools._ctl_followup({"op": "add", "kind": "review",
                                     "text": "sweep logs", "scope_task": "tk",
                                     "period_s": 600, "times": 30})
    assert '"ok": true' in out
    row = conn.execute("SELECT max_repeats FROM ctl_followup").fetchone()
    assert row["max_repeats"] == 30  # 12h/600s=72 fits, 30 <= 72

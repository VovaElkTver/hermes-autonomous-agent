"""B4 (TZ-2026-11-16-dsh-B): RFC 5545 RRULE for ctl_followup.

The carrier stays the event row: an rrule row is re-plated in place (id
stable, no delete+insert) to its next occurrence by the ticker. Covers the
mini-parser (FREQ DAILY|WEEKLY|MONTHLY, INTERVAL, BYDAY; no COUNT), the
re-plate on the agent and user paths, ``next_due`` in ``list_open``, and
the two-step schema migration for pre-B4 databases.
"""

import calendar
import sqlite3
import time

import pytest

from controller import followups, model
from controller.rrule import RRuleError, next_due_at, parse_rrule
from hermes_state_common import SCHEMA_SQL

#: A known Friday: 2026-11-20 09:00 UTC (the TZ acceptance anchor).
FRI = calendar.timegm((2026, 11, 20, 9, 0, 0, 0, 0))
MON_1 = calendar.timegm((2026, 11, 23, 9, 0, 0, 0, 0))
MON_2 = calendar.timegm((2026, 11, 30, 9, 0, 0, 0, 0))


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "state.db")
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA_SQL)
    yield c
    c.close()


@pytest.fixture()
def scope_task(conn, tmp_path):
    return model.create_task(conn, owner="vovak", workdir=str(tmp_path),
                             goal="корневая задача темы", project_id="proj-1")


class _Deliver:
    def __init__(self):
        self.sent: list[str] = []

    def __call__(self, receipt):
        self.sent.append(receipt["text"])
        return "sent"


def _row(conn, fid):
    return dict(conn.execute(
        "SELECT * FROM ctl_followup WHERE followup_id=?", (fid,)).fetchone())


# --- the mini-parser (TZ acceptance: Fri -> Mon; INTERVAL=2 -> two Mon) -----

class TestParse:
    def test_weekly_byday_from_friday(self):
        """TZ acceptance, verbatim: ``FREQ=WEEKLY;BYDAY=MO`` от пятницы → пн."""
        assert next_due_at("FREQ=WEEKLY;BYDAY=MO", FRI, anchor_ts=FRI) == MON_1

    def test_weekly_byday_interval_two(self):
        """``INTERVAL=2`` — через пн (skip a Monday, land on the next one)."""
        assert next_due_at("FREQ=WEEKLY;INTERVAL=2;BYDAY=MO", FRI, anchor_ts=FRI) == MON_2

    def test_rrule_prefix_accepted(self):
        assert next_due_at("RRULE:FREQ=WEEKLY;BYDAY=MO", FRI, anchor_ts=FRI) == MON_1

    def test_daily_interval_keeps_time_of_day(self):
        out = next_due_at("FREQ=DAILY;INTERVAL=3", FRI, anchor_ts=FRI)
        assert out > FRI
        assert out - FRI == 3 * 86_400
        assert time.gmtime(out).tm_hour == time.gmtime(FRI).tm_hour

    def test_strictly_after(self):
        # sitting exactly on an occurrence moves to the NEXT one
        assert next_due_at("FREQ=WEEKLY;BYDAY=MO", MON_1, anchor_ts=FRI) == MON_2

    def test_monthly_same_day_of_month(self):
        jan31 = calendar.timegm((2026, 1, 31, 8, 0, 0, 0, 0))
        out = next_due_at("FREQ=MONTHLY", jan31, anchor_ts=jan31)
        assert time.gmtime(out).tm_mon == 3 and time.gmtime(out).tm_mday == 31  # Feb lacks the 31st

    def test_multi_byday_picks_soonest(self):
        # From Friday, this week's MO/WE are both in the past: the next cycle's
        # Monday comes before its Wednesday.
        out = next_due_at("FREQ=WEEKLY;BYDAY=WE,MO", FRI, anchor_ts=FRI)
        assert out == MON_1

    def test_defaults(self):
        spec = parse_rrule("FREQ=DAILY")
        assert spec == {"freq": "DAILY", "interval": 1, "byday": ()}

    def test_count_rejected_explicitly(self):
        with pytest.raises(RRuleError, match="COUNT"):
            parse_rrule("FREQ=WEEKLY;COUNT=5")

    def test_unknown_part_rejected(self):
        with pytest.raises(RRuleError, match="UNTIL"):
            parse_rrule("FREQ=DAILY;UNTIL=20261201T000000Z")

    def test_byday_weekly_only(self):
        with pytest.raises(RRuleError, match="WEEKLY-only"):
            parse_rrule("FREQ=DAILY;BYDAY=MO")

    def test_bad_freq_rejected(self):
        with pytest.raises(RRuleError):
            parse_rrule("FREQ=HOURLY")

    def test_bad_weekday_rejected(self):
        with pytest.raises(RRuleError):
            parse_rrule("FREQ=WEEKLY;BYDAY=MT")

    def test_bad_interval_rejected(self):
        with pytest.raises(RRuleError):
            parse_rrule("FREQ=DAILY;INTERVAL=0")


# --- add() validation ---------------------------------------------------------

class TestAdd:
    def test_review_rrule_requires_due_at(self, conn, scope_task):
        with pytest.raises(ValueError, match="due_at"):
            followups.add(conn, kind="review", scope_task=scope_task, text="обзор",
                          rrule="FREQ=WEEKLY;BYDAY=MO", now=0.0)

    def test_review_needs_period_or_rrule(self, conn, scope_task):
        with pytest.raises(ValueError, match="period_s or rrule"):
            followups.add(conn, kind="review", scope_task=scope_task, text="обзор", now=0.0)

    def test_typo_rrule_fails_early(self, conn, scope_task):
        with pytest.raises(RRuleError, match="COUNT"):
            followups.add(conn, kind="review", scope_task=scope_task, text="обзор",
                         due_at=FRI, rrule="FREQ=WEEKLY;COUNT=3", now=0.0)

    def test_rrule_stored_on_row(self, conn, scope_task):
        fid = followups.add(conn, kind="review", scope_task=scope_task, text="обзор",
                            due_at=FRI, rrule="FREQ=WEEKLY;BYDAY=MO", now=0.0)
        assert _row(conn, fid)["rrule"] == "FREQ=WEEKLY;BYDAY=MO"


# --- agent path: re-plate, id stable -----------------------------------------

class TestAgentReplate:
    def test_review_rrule_replates_same_row(self, conn, scope_task):
        fid = followups.add(conn, kind="review", scope_task=scope_task, text="обзор PR",
                            due_at=FRI, rrule="FREQ=WEEKLY;BYDAY=MO", now=0.0)
        d = _Deliver()
        assert followups.scan(conn, d, now=FRI) == [fid]      # first occurrence fired
        row = _row(conn, fid)
        assert row["state"] == "pending" and row["dispatch_ref"]
        assert row["due_at"] == MON_1                          # re-plated in place
        assert followups.scan(conn, d, now=MON_1 - 3600) == []  # not early
        model.set_task_state(conn, row["dispatch_ref"], "completed")
        assert followups.scan(conn, d, now=MON_1) == [fid]    # next occurrence
        row2 = _row(conn, fid)
        assert row2["due_at"] == MON_2                          # series continues
        assert len(_row_ids(conn, fid)) == 1                   # id stable, no new row
        assert len(_dispatched(conn)) == 2

    def test_reminder_rrule_replates_instead_of_done(self, conn, scope_task):
        fid = followups.add(conn, kind="reminder", scope_task=scope_task, text="check PR",
                            due_at=FRI, rrule="FREQ=WEEKLY;BYDAY=MO", now=0.0)
        d = _Deliver()
        assert followups.scan(conn, d, now=FRI) == [fid]
        ref = _row(conn, fid)["dispatch_ref"]
        model.set_task_state(conn, ref, "completed")
        # the previous work is terminal → the series re-plates in place
        # (no new task this scan — the row is re-armed, it does not fire again)
        assert followups.scan(conn, d, now=MON_1) == []
        row = _row(conn, fid)
        assert row["state"] == "pending" and row["due_at"] == MON_2
        assert row["dispatch_ref"] is None and row["retries"] == 0
        assert followups.scan(conn, d, now=MON_2) == [fid]   # the series fires on

    def test_reminder_rrule_holds_while_prev_open(self, conn, scope_task):
        fid = followups.add(conn, kind="reminder", scope_task=scope_task, text="echo",
                            due_at=FRI, rrule="FREQ=WEEKLY;BYDAY=MO", now=0.0)
        d = _Deliver()
        followups.scan(conn, d, now=FRI)
        ref = _row(conn, fid)["dispatch_ref"]
        assert followups.scan(conn, d, now=MON_1) == []   # prev task still open → hold
        assert len(_dispatched(conn)) == 1                 # never piles up
        model.set_task_state(conn, ref, "completed")
        assert followups.scan(conn, d, now=MON_1) == []    # re-plate (no fire)
        assert _row(conn, fid)["due_at"] == MON_2
        assert followups.scan(conn, d, now=MON_2) == [fid]

    def test_legacy_review_untouched(self, conn, scope_task):
        """No rrule: the period arithmetic path is byte-identical (id stable)."""
        fid = followups.add(conn, kind="review", scope_task=scope_task, text="обзор",
                            period_s=60, now=0.0)
        d = _Deliver()
        assert followups.scan(conn, d, now=61.0) == [fid]
        assert _row(conn, fid)["rrule"] is None


# --- user path: re-plate -------------------------------------------------------

class TestUserReplate:
    def test_user_review_rrule_replates(self, conn, scope_task):
        fid = followups.add(conn, kind="review", scope_task=scope_task, text="обзор для человека",
                            due_at=FRI, rrule="FREQ=WEEKLY;BYDAY=MO", recipient="user", now=0.0)
        d = _Deliver()
        assert followups.scan(conn, d, now=FRI) == [fid]
        assert len(d.sent) == 1
        assert _row(conn, fid)["due_at"] == MON_1            # re-plated, same id
        assert followups.scan(conn, d, now=MON_1) == [fid]
        assert _row(conn, fid)["due_at"] == MON_2

    def test_user_reminder_rrule_replates_on_exhaustion(self, conn, scope_task):
        fid = followups.add(conn, kind="reminder", scope_task=scope_task, text="ping",
                            due_at=FRI, rrule="FREQ=WEEKLY;BYDAY=MO", recipient="user",
                            max_repeats=1, now=0.0)
        d = _Deliver()
        assert followups.scan(conn, d, now=FRI) == [fid]          # fired, retries=1
        assert len(d.sent) == 1
        # repeat window (300s) passed: retries >= max_repeats → the series
        # re-plates instead of the exhaustion notice (the row is NOT done)
        assert followups.scan(conn, d, now=FRI + 300) == []
        row = _row(conn, fid)
        assert row["state"] == "pending" and row["due_at"] == MON_1
        assert row["retries"] == 0
        assert "не дозвонилось" not in "".join(d.sent)


# --- list_open prints next_due --------------------------------------------------

class TestNextDue:
    def test_next_due_prints_per_row(self, conn, scope_task):
        fid_rrule = followups.add(conn, kind="review", scope_task=scope_task, text="rr",
                                  due_at=FRI, rrule="FREQ=WEEKLY;BYDAY=MO", now=0.0)
        fid_period = followups.add(conn, kind="review", scope_task=scope_task, text="per",
                                   period_s=60, now=0.0)
        fid_rem = followups.add(conn, kind="reminder", scope_task=scope_task, text="rem",
                                due_at=FRI, now=0.0)
        listing = {r["followup_id"]: r for r in followups.list_open(conn, scope_task)}
        assert listing[fid_rrule]["next_due"] == MON_1
        assert listing[fid_period]["next_due"] == 60.0
        assert listing[fid_rem]["next_due"] == FRI


# --- two-step migration ----------------------------------------------------------

class TestMigration:
    def test_reconcile_adds_rrule_column(self, tmp_path):
        """A pre-B4 database (the table without rrule) migrates by the
        declarative SCHEMA_SQL + reconcile column pass — no backfill."""
        from hermes_state_schema import reconcile_state_schema
        from hermes_state_common import DEFERRED_INDEX_SQL

        db = tmp_path / "state.db"
        c = sqlite3.connect(db)
        c.row_factory = sqlite3.Row
        # the pre-B4 shape: the ctl_followup table without the rrule column
        c.executescript(
            """
            CREATE TABLE ctl_followup (
              followup_id TEXT PRIMARY KEY,
              kind TEXT NOT NULL CHECK (kind IN ('reminder','review')),
              scope_task TEXT NOT NULL,
              scope_project TEXT,
              text TEXT NOT NULL,
              due_at REAL,
              period_s INTEGER,
              state TEXT NOT NULL DEFAULT 'pending'
                   CHECK (state IN ('pending','awaiting_ack','done','cancelled')),
              recipient TEXT NOT NULL DEFAULT 'agent'
                   CHECK (recipient IN ('agent','user')),
              dispatch_ref TEXT,
              last_fired_at REAL,
              retries INTEGER NOT NULL DEFAULT 0,
              repeat_beats INTEGER NOT NULL DEFAULT 300,
              max_repeats INTEGER NOT NULL DEFAULT 3,
              created_by TEXT NOT NULL,
              created_at REAL NOT NULL,
              provenance_json TEXT
            );
            """)
        c.execute("INSERT INTO ctl_followup (followup_id, kind, scope_task, text, created_by, created_at)"
                  " VALUES ('fup-old', 'reminder', 'task-x', 'legacy row', 'agent', 0.0)")
        c.commit()
        c.close()

        c = sqlite3.connect(db)
        c.row_factory = sqlite3.Row
        try:
            reconcile_state_schema(c)
            cols = [r["name"] for r in c.execute("PRAGMA table_info(ctl_followup)").fetchall()]
            assert "rrule" in cols
            row = dict(c.execute("SELECT * FROM ctl_followup WHERE followup_id='fup-old'").fetchone())
            assert row["rrule"] is None                  # additive: existing rows untouched
            # and the B4 code path reads the column without complaint
            followups.ensure_schema(c)
            listing = followups.list_open(c, "task-x")
            assert listing[0]["rrule"] is None
        finally:
            c.close()


def _row_ids(conn, fid):
    return [r[0] for r in conn.execute(
        "SELECT followup_id FROM ctl_followup WHERE followup_id=?", (fid,)).fetchall()]


def _dispatched(conn):
    return conn.execute(
        "SELECT * FROM ctl_task WHERE goal LIKE '[followup %' ORDER BY created_at").fetchall()

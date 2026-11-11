"""tasks_kernel v0.2 mechanisms (Вл 10.11 «правь все три»):

1. fanout — competing branch proposals + a LAST join row carrying the digest;
2. open-question register — a task with open questions cannot be ack-closed;
3. proximity dedup — near-duplicate branches refused at fan-out time.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

import tasks_kernel as tk

CHAT = "!room:host"


@pytest.fixture()
def db(tmp_path):
    return str(tmp_path / "state.db")


@pytest.fixture()
def lines(monkeypatch):
    """Capture FULL delivery lines (not just titles like the v0 fixture)."""
    out: list[str] = []

    def _fake(task: dict) -> bool:
        out.append(task.get("_line") or f"{task['title']}: {task.get('body', '')}")
        return True

    monkeypatch.setattr(tk, "deliver_fn", _fake)
    return out


# ---------------------------------------------------------------- mechanism 1

def test_fanout_creates_branches_and_trailing_join(db, lines):
    res = tk.fanout(CHAT, "как греть WAL", [
        {"title": "через watchdog", "body": "рестарт по heartbeat"},
        {"title": "BEGIN IMMEDIATE", "body": "rollback в каждом except"},
    ], criteria="надёжность при минимуме кода",
        deliver_target="matrix:!room:host", db_path=db)
    assert len(res["branch_ids"]) == 2
    st = tk.status(CHAT, db_path=db)
    titles = [t["title"] for t in st["tasks"]]
    assert titles[-1].startswith("СЛИЯНИЕ")
    tk.start(CHAT, db_path=db)
    assert "[ветка 1/2]" in lines[0] and "watchdog" in lines[0]


def test_join_digest_lists_branches_criteria_and_verdicts(db, lines):
    res = tk.fanout(CHAT, "цель", [
        {"title": "альфа", "body": "идея А"},
        {"title": "бета", "body": "идея Б"},
    ], criteria="цена внедрения", deliver_target="t", db_path=db)
    tk.start(CHAT, db_path=db)                    # branch 1 delivered
    tk.annotate(res["branch_ids"][0], "А падает на 1M строк", db_path=db)
    tk.on_owner_message(CHAT, db_path=db)         # close 1, deliver 2
    last = tk.on_owner_message(CHAT, db_path=db)  # close 2 → join delivered
    assert last["title"].startswith("СЛИЯНИЕ")
    digest = lines[-1]
    assert "альфа" in digest and "бета" in digest
    assert "цена внедрения" in digest
    assert "вывод: А падает на 1M строк" in digest
    assert "решение" in digest
    # after the join ack the queue is done
    tk.on_owner_message(CHAT, db_path=db)
    assert tk.status(CHAT, db_path=db)["remaining"] == 0


def test_fanout_requires_two_branches(db):
    with pytest.raises(ValueError):
        tk.fanout(CHAT, "g", [{"title": "одна"}], db_path=db)


# ---------------------------------------------------------------- mechanism 3

def test_near_duplicate_branches_refused(db, lines):
    with pytest.raises(ValueError, match="near-duplicates"):
        tk.fanout(CHAT, "g", [
            {"title": "кэш отпечатков в sqlite", "body": "ledger строк"},
            {"title": "кэш отпечатков в sqlite", "body": "ledger строк"},
        ], db_path=db)


def test_similarity_seam_can_be_disabled(db, lines, monkeypatch):
    monkeypatch.setattr(tk, "similarity_fn", lambda a, b: 0.0)
    res = tk.fanout(CHAT, "g", [
        {"title": "X", "body": "same"},
        {"title": "X", "body": "same"},
    ], db_path=db)
    assert len(res["branch_ids"]) == 2


# ---------------------------------------------------------------- mechanism 2

def _mk(db, tid_title="head"):
    ids = tk.create_batch(CHAT, [{"title": tid_title}], db_path=db)
    tk.start(CHAT, db_path=db)
    return ids[0]


def test_open_questions_block_completion(db, lines):
    tid = _mk(db)
    assert tk.add_question(tid, "кто продюсер события?", db_path=db) == 1
    r = tk.on_owner_message(CHAT, db_path=db)
    assert r["refused_open_questions"] == 1
    st = tk.status(CHAT, db_path=db)
    assert st["tasks"][0]["state"] == tk.WAIT_HUMAN        # NOT closed
    assert "открытых вопросов" in lines[-1] or "вопросов" in lines[-1]
    assert tk.drop_question(tid, 1, db_path=db) == 0
    tk.on_owner_message(CHAT, db_path=db)
    assert tk.status(CHAT, db_path=db)["remaining"] == 0   # now closes


def test_drop_question_by_substring(db, lines):
    tid = _mk(db)
    tk.add_question(tid, "проверить лицензию донора", db_path=db)
    assert tk.drop_question(tid, "лицензию", db_path=db) == 0


def test_drop_missing_question_raises(db, lines):
    tid = _mk(db)
    with pytest.raises(ValueError):
        tk.drop_question(tid, "нет такого", db_path=db)


def test_skip_still_works_with_open_questions(db, lines):
    tk.create_batch(CHAT, [{"title": "a"}, {"title": "b"}], db_path=db)
    tk.start(CHAT, db_path=db)
    head = tk.status(CHAT, db_path=db)["tasks"][0]
    tk.add_question(head["task_id"], "q?", db_path=db)
    r = tk.skip(CHAT, db_path=db)                          # postpone allowed
    assert r["skipped"]["title"] == "a" and r["delivered"]["title"] == "b"


def test_status_exposes_open_questions(db, lines):
    tid = _mk(db, "s")
    tk.add_question(tid, "вопрос", db_path=db)
    row = tk.status(CHAT, db_path=db)["tasks"][0]
    assert json.loads(row["open_q"]) == ["вопрос"]


# ------------------------------------------------------------- schema/migration

def test_v01_database_migrates_in_place(db, lines):
    """An old queue (no parent_id/open_q columns) upgrades additively."""
    conn = sqlite3.connect(db)
    conn.executescript("""
CREATE TABLE tk_task (
    task_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, seq INTEGER NOT NULL,
    title TEXT NOT NULL, body TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'ready', wait_event TEXT, note TEXT,
    created_at REAL NOT NULL, updated_at REAL NOT NULL);
INSERT INTO tk_task VALUES ('old1','!room:host',1,'legacy','x','wait_human',
    NULL,'{"deliver_target":"matrix:!room:host"}',0,0);
""")
    conn.commit()
    conn.close()
    assert tk.on_owner_message(CHAT, db_path=db) is None    # drained cleanly
    st = tk.status(CHAT, db_path=db)
    assert st["tasks"][0]["state"] == tk.DONE


# ------------------------------------------------------------------ tool layer

def test_tool_actions_fanout_and_questions(tmp_path, monkeypatch, lines, db):
    monkeypatch.setenv("HERMES_STATE_DB", db)
    from tools.task_tools import task_queue
    r = task_queue({"action": "fanout", "chat_id": CHAT, "goal": "цель",
                    "branches": [{"title": "А", "body": "первый путь"},
                                 {"title": "Б", "body": "второй путь"}],
                    "criteria": "скорость"})
    assert r["ok"] and len(r["branch_ids"]) == 2
    tid = r["branch_ids"][0]
    assert task_queue({"action": "add_question", "task_id": tid,
                       "question": "а точно? "})["open"] == 1
    assert task_queue({"action": "drop_question", "task_id": tid,
                       "question": "1"})["open"] == 0
    bad = task_queue({"action": "fanout", "chat_id": CHAT, "goal": "g",
                      "branches": [{"title": "тот же"}, {"title": "тот же"}]})
    assert not bad["ok"] and "near-duplicate" in bad["error"]

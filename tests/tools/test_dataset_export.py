"""Tests for tools/dataset_export.py — fixture-driven, in-memory schema."""

from __future__ import annotations

import json
import sqlite3
import sys
import pathlib
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "tools"))

from dataset_export import (
    export_sharegpt,
    session_to_conversation,
    validate,
)

SCHEMA = """
create table messages (
    id integer primary key, session_id text, role text, content text,
    tool_calls text, tool_name text, timestamp real,
    active integer default 1, compacted integer default 0
);
create table sessions (
    id text primary key, system_prompt text
);
"""


def seed(rows, sp="you are a helpful agent"):
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    con.execute("insert into sessions (id, system_prompt) values (?,?)", ("s1", sp))
    for i, (role, content, tc, ts) in enumerate(rows):
        con.execute(
            "insert into messages (session_id, role, content, tool_calls,"
            " tool_name, timestamp, active, compacted)"
            " values ('s1',?,?,?,?,?,1,0)",
            (role, content, tc, None, ts),
        )
    return con


class FakeRow(dict):
    __getattr__ = dict.get


def mk(role, content, calls=None, active=1, compacted=0):
    r = dict(session_id="s1", role=role, content=content,
             tool_calls=json.dumps(calls) if calls else None,
             active=active, compacted=compacted)
    return FakeRow(r)


def test_full_tool_loop_trajectory():
    call = {"function": {"name": "grep", "arguments": '{"p": "x"}'}}
    rows = [
        mk("user", "find the needle"),
        mk("assistant", "", [call]),
        mk("tool", "needle at line 3"),
        mk("assistant", "found it"),
    ]
    conv = session_to_conversation("SYS", rows, has_flags=True)
    f = [c["from"] for c in conv["conversations"]]
    # empty-text call carrier yields ONLY function_call (no empty gpt)
    assert f == ["system", "human", "function_call", "observation", "gpt"]
    assert validate(conv)


def test_orphan_observation_skipped():
    rows = [
        mk("user", "hi"),
        mk("tool", "orphan result"),  # no preceding gpt-with-call
        mk("assistant", "done"),
    ]
    conv = session_to_conversation("", rows, has_flags=True)
    f = [c["from"] for c in conv["conversations"]]
    assert "observation" not in f
    assert validate(conv)


def test_dangling_call_stripped():
    call = {"function": {"name": "t", "arguments": "{}"}}
    rows = [mk("user", "q"), mk("assistant", "", [call])]
    conv = session_to_conversation("", rows, has_flags=True)
    # trailing function_call without observation is stripped; left with human only
    assert conv is None or all(c["from"] != "function_call" for c in conv["conversations"])


def test_compacted_rows_excluded():
    dead = mk("assistant", "old ghosts", active=1, compacted=1)
    live = mk("assistant", "live answer")
    conv = session_to_conversation("", [mk("user", "q"), dead, live], has_flags=True)
    values = [c["value"] for c in conv["conversations"]]
    assert "old ghosts" not in " ".join(values)
    assert "live answer" in values


def test_session_without_assistant_rejected():
    rows = [mk("user", "hello?")]
    assert session_to_conversation("", rows, has_flags=True) is None


def test_export_sharegpt_end_to_end(tmp_path):
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
    con.execute("insert into sessions values ('sA','sys prompt here')")
    triplets = [
        ("user", "what?", None, 100.0),
        ("assistant", "answer!", None, 101.0),
    ]
    for role, c, tc, ts in triplets:
        con.execute(
            "insert into messages (session_id, role, content, tool_calls, timestamp)"
            " values ('sA',?,?,?,?)",
            (role, c, tc, ts),
        )
    con.commit()
    con.close()
    out = export_sharegpt(db)
    assert len(out) == 1
    conv = out[0]
    assert validate(conv)
    assert conv["conversations"][0]["value"] == "sys prompt here"

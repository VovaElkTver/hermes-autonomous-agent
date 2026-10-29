"""Regression: a state.db write-lock timeout names the process holding the lock.

Before, ``database is locked (another Hermes process held the state.db write lock for over 60s)``
identified the victim only; every Hermes process has the DB open, so the descriptor scan could not
single out the writer. ``/proc/locks`` can.
"""

import io
import os
import sqlite3
import subprocess
import sys
import textwrap
import time

import pytest

from hermes_state_lockowners import parse_proc_locks, state_db_write_lock_holders


def test_parse_proc_locks_keeps_only_write_locks_on_our_inodes_and_decodes_the_wal_write_byte():
    inodes = {(os.makedev(0x103, 0x02), 4194737): "-shm", (os.makedev(0x103, 0x02), 4228330): ""}
    text = textwrap.dedent("""\
        1: POSIX  ADVISORY  WRITE 594094 103:02:4194737 120 120
        2: POSIX  ADVISORY  READ 594094 103:02:4194737 125 125
        3: POSIX  ADVISORY  READ 99493 103:02:4194737 128 128
        4: OFDLCK ADVISORY  WRITE -1 103:02:4228330 1073741825 1073741825
        5: POSIX  ADVISORY  WRITE 4242 103:02:99999 120 120
        6: -> POSIX  ADVISORY  WRITE 777 103:02:4194737 120 120
    """)
    assert parse_proc_locks(text, inodes) == [
        (594094, "WAL write", "-shm"),
        (-1, "RESERVED", ""),
    ]


@pytest.mark.platforms("linux")
def test_holder_skipped_by_one_proc_locks_pass_is_still_named(tmp_path, monkeypatch):
    """/proc/locks is served over several read()s, so churn elsewhere can skip an entry in one pass."""
    import hermes_state_lockowners

    db = tmp_path / "state.db"
    db.write_bytes(b"")
    st = os.stat(db)
    held = (f"1: POSIX  ADVISORY  WRITE {os.getpid()} "
            f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino} 1073741825 1073741825\n")
    passes = iter(["", held, ""])
    monkeypatch.setattr(hermes_state_lockowners, "open", lambda *a, **k: io.StringIO(next(passes)), raising=False)
    lines = state_db_write_lock_holders(db)
    assert len(lines) == 1 and f"PID {os.getpid()} " in lines[0] and "RESERVED" in lines[0], lines


@pytest.mark.platforms("linux")
def test_live_writer_in_another_process_is_named_by_pid(tmp_path):
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=wal")
    conn.execute("CREATE TABLE t(x)")
    conn.commit()
    conn.close()

    holder = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(f"""
            import sqlite3, sys, time
            c = sqlite3.connect({str(db)!r}, isolation_level=None)
            c.execute("BEGIN IMMEDIATE")
            c.execute("INSERT INTO t VALUES (1)")
            print("held", flush=True)
            time.sleep(30)
        """)],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        deadline = time.monotonic() + 5
        lines = []
        while time.monotonic() < deadline:
            lines = state_db_write_lock_holders(db)
            if lines:
                break
            time.sleep(0.05)
        assert any(f"PID {holder.pid} " in line and "WAL write" in line for line in lines), lines
    finally:
        holder.kill()
        holder.wait()
    assert state_db_write_lock_holders(db) == []


def test_self_held_lock_appends_writer_thread_candidates(caplog, tmp_path, monkeypatch):
    """/proc/locks stops at the pid; when the holder is this process the WARNING must also name
    the candidate writer threads, and must not list the waiting writer itself."""
    import logging

    import hermes_state_lockowners as hsl

    seen = {}

    def fake_holders(_db):
        return [f"PID {os.getpid()} (hermes gateway run) holds WAL write lock on state.db-shm"]

    def fake_candidates(exclude_tid=None, **_kw):
        seen["exclude_tid"] = exclude_tid
        return ["[cron-scheduler] _run@scheduler_thread.py:34"]

    monkeypatch.setattr(hsl, "state_db_write_lock_holders", fake_holders)
    monkeypatch.setattr(hsl, "candidate_writer_threads", fake_candidates)
    with caplog.at_level(logging.WARNING, logger="hermes_state_lockowners"):
        hsl.log_write_lock_holders(tmp_path / "state.db", 20)
    msg = caplog.text
    assert "same-PID writer candidates: [cron-scheduler]" in msg
    assert seen["exclude_tid"] == threading_get_ident()


def threading_get_ident():
    import threading

    return threading.get_ident()


@pytest.mark.platforms("linux")
def test_candidate_writer_threads_finds_a_parked_holder_and_excludes_the_caller(tmp_path):
    """A thread parked inside a state.db-named module under BEGIN IMMEDIATE is listed; passing
    its ident as exclude_tid drops it again."""
    import importlib.util
    import threading

    from hermes_state_lockowners import candidate_writer_threads

    sim = tmp_path / "hermes_state_sim.py"
    sim.write_text(
        "import sqlite3\n"
        "def hold(db, ev):\n"
        "    conn = sqlite3.connect(db)\n"
        "    conn.execute('BEGIN IMMEDIATE')\n"
        "    conn.execute('CREATE TABLE t(x)')\n"
        "    ev.wait(10)\n"
    )
    spec = importlib.util.spec_from_file_location("hermes_state_sim", sim)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    ev = threading.Event()
    t = threading.Thread(target=mod.hold, args=(str(tmp_path / "x.db"), ev), name="writer-probe")
    t.start()
    try:
        time.sleep(0.3)
        found = candidate_writer_threads()
        assert any("writer-probe" in line for line in found), found
        assert not any("writer-probe" in line for line in candidate_writer_threads(exclude_tid=t.ident))
    finally:
        ev.set()
        t.join(timeout=5)

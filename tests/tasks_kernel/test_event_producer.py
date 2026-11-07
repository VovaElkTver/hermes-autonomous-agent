"""Event-producer E2E: external script wakes wait_event tasks without an agent turn."""
from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

import tasks_kernel as tk

CHAT = "!room:host"
_SCRIPT = pathlib.Path(tk.__file__).resolve().parent.parent / "scripts" / "task_emit.py"


def _seed_wait_event(db, title, event):
    """Park a task the way real wait_event producers do: state=wait_human."""
    tk.create_batch(CHAT, [{"title": title}], db_path=db)
    with tk._connect(db) as conn:
        conn.execute("UPDATE tk_task SET state=?, wait_event=?,"
                     " note='{\"deliver_target\": \"\"}' WHERE title=?",
                     (tk.WAIT_HUMAN, event, title))
        conn.commit()


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_STATE_DB", str(tmp_path / "state.db"))
    return str(tmp_path / "state.db")


@pytest.fixture()
def sent(monkeypatch):
    seen: list[str] = []

    def _fake(task: dict) -> bool:
        seen.append(task["title"])
        return True

    monkeypatch.setattr(tk, "deliver_fn", _fake)
    return seen


def test_script_exists():
    assert _SCRIPT.exists(), f"missing {_SCRIPT}"


def test_emit_wakes_all_subscribers(db, sent):
    _seed_wait_event(db, "digest", "evening-tick")
    _seed_wait_event(db, "backup", "evening-tick")
    assert tk.emit("evening-tick", db_path=db) == 2
    st = {t["title"]: t["state"] for t in tk.status(CHAT, db_path=db)["tasks"]}
    assert st["digest"] == tk.WAIT_HUMAN          # delivered, parked again
    assert st["backup"] == tk.WAIT_HUMAN
    assert sent == ["digest", "backup"]           # cyclic: redelivered on event


def test_emit_zero_subscribers(db, sent):
    assert tk.emit("nobody-listens", db_path=db) == 0


def test_script_wrapper_wiring(db, tmp_path):
    """The out-of-process path: script finds the db, fires, reports honestly."""
    _seed_wait_event(db, "digest", "evening-tick")   # empty deliver_target → no-op delivery
    r = subprocess.run([sys.executable, str(_SCRIPT), "evening-tick", "--db", db],
                       capture_output=True, text=True, timeout=60,
                       env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "event evening-tick" in out
    # second subscriber absent → honest zero, not a fabricated success
    r = subprocess.run([sys.executable, str(_SCRIPT), "ghost-event", "--db", db],
                       capture_output=True, text=True, timeout=60,
                       env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
    assert r.returncode == 0 and "resumed 0" in (r.stdout or "")

"""Tests for the /dream command — thin gateway adapter to the outer dream0 contour.

Covers: handler registration in _IDLE_COMMANDS (dead-name regression discipline),
go/status/last flows, single-live-run guard with stale-PID recovery, and ledger
parsing. The contour itself lives outside the repo; here it is faked via tmp_home
and a fake Popen.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from gateway.slash_commands_goals import GatewayGoalCommandsMixin
from gateway.platforms.event import MessageEvent, MessageType


# --------------------------------------------------------------------- helpers
class _Src:
    def __init__(self, chat_id="!test:mx.elk.mobi"):
        self.platform = type("P", (), {"name": "matrix"})()
        self.chat_id = chat_id


def _event(args=""):
    ev = MessageEvent.__new__(MessageEvent)
    ev.source = _Src()
    ev.message_type = MessageType.COMMAND
    ev.text = "/dream" + (f" {args}" if args else "")
    return ev


@pytest.fixture
def dream_env(tmp_path, monkeypatch):
    """Redirect HOME to tmp: ledger under projects/, run-state under .hermes/."""
    home = tmp_path / "home"
    script_dir = home / "projects" / "outer-contour" / "dream0"
    script_dir.mkdir(parents=True)
    script = script_dir / "dream0.py"
    script.write_text("#!/usr/bin/env python3\nprint('fake')\n", encoding="utf-8")
    (home / ".hermes" / "logs").mkdir(parents=True)
    (home / ".hermes" / "cache" / "scratch").mkdir(parents=True)
    import pathlib
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: home), raising=False)
    return home


class _Proc:
    def __init__(self, pid=4242):
        self.pid = pid


def test_dream_registered_in_idle_commands():
    from gateway.run import GatewayRunner
    runner = GatewayRunner.__new__(GatewayRunner)
    assert "dream" in runner._IDLE_COMMANDS
    assert callable(runner._handle_dream_command)


def test_dream_status_empty_ledger(dream_env):
    mixin = object.__new__(GatewayGoalCommandsMixin)
    reply = asyncio.run(GatewayGoalCommandsMixin._handle_dream_command(mixin, _event()))
    assert "[dream] леджер" in reply


def test_dream_go_spawns_detached_and_writes_state(dream_env, monkeypatch):
    spawned = {}

    def fake_popen(cmd, **kw):
        spawned["cmd"] = cmd
        return _Proc()

    monkeypatch.setattr("gateway.slash_commands_goals.subprocess.Popen", fake_popen)
    mixin = object.__new__(GatewayGoalCommandsMixin)
    reply = asyncio.run(
        GatewayGoalCommandsMixin._handle_dream_command(mixin, _event("go 2")))
    assert "сон запущен" in reply
    assert "--stage" in spawned["cmd"] and "--span 2" in " ".join(spawned["cmd"])
    state = json.loads((dream_env / ".hermes" / "cache" / "scratch" / "dream_run.json")
                       .read_text(encoding="utf-8"))
    assert state["pid"] == 4242 and state["span"] == 2


def test_dream_go_refuses_second_live_run(dream_env, monkeypatch):
    monkeypatch.setattr("gateway.slash_commands_goals.subprocess.Popen",
                        lambda *a, **kw: _Proc(pid=999998))
    # pre-seed state pointing at a live process (os.kill(pid,0) succeeds on self-ish pid)
    pid_file = dream_env / ".hermes" / "cache" / "scratch" / "dream_run.json"
    import os as _os
    pid_file.write_text(json.dumps({"pid": _os.getpid(), "started": "x", "span": 1}),
                        encoding="utf-8")
    mixin = object.__new__(GatewayGoalCommandsMixin)
    reply = asyncio.run(
        GatewayGoalCommandsMixin._handle_dream_command(mixin, _event("go")))
    assert "уже идёт" in reply


def test_dream_last_reports_wake_verdicts(dream_env):
    ledger = dream_env / "projects" / "outer-contour" / "dream0" / "data" / "dream-ledger.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(
        json.dumps({"ts": "T1", "kind": "collected", "day": "20261112"}) + "\n"
        + json.dumps({"ts": "T2", "kind": "woke", "day": "20261112", "verified": 10,
                      "refuted": 1, "probe_error": 0, "cards": 5, "cards_total": 11}) + "\n",
        encoding="utf-8")
    mixin = object.__new__(GatewayGoalCommandsMixin)
    reply = asyncio.run(
        GatewayGoalCommandsMixin._handle_dream_command(mixin, _event("last")))
    assert "verified 10" in reply and "день 20261112" in reply

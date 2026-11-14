"""Regression tests for the Windows hard-reboot branch (/reset on native Windows).

Bug (WSL diagnosis 14.11): ``_hard_reboot`` shelled out ``systemd-run --user /bin/bash
scripts/hard_reboot.sh`` unconditionally; native Windows has neither, so every
``/reset`` / ``/reset-safe`` died with ``FileNotFoundError [WinError 2]`` before the
restart ever started. The fix routes ``sys.platform == "win32"`` to the detached
restart watcher ``hermes update`` already uses, writes the safe-boot flag the bash
script would have touched, and self-kills via a 2 s timer.
"""
from __future__ import annotations

import sys

import pytest

from gateway.run_busy import GatewayBusySessionMixin


class _Stub(GatewayBusySessionMixin):
    def __init__(self):  # bypass the heavy gateway mixin init
        pass


@pytest.fixture()
def win(monkeypatch, tmp_path):
    """Pretend to be win32; capture watcher spawns, flag writes and timer arming.

    All imports happen BEFORE the platform flip: gateway.status selects ``msvcrt`` by
    ``sys.platform`` at module-import time, which Linux does not have."""
    calls: dict = {"spawn": None, "timers": [], "popen": []}

    import hermes_cli.gateway as hg
    import agent.safe_mode as sm
    import threading
    import subprocess

    def fake_launch(old_pid, run_argv, home=None):
        calls["spawn"] = {"old_pid": old_pid, "run_argv": run_argv, "home": home}
        return calls.get("spawn_ok", True)

    monkeypatch.setattr(hg, "launch_detached_gateway_restart_by_cmdline", fake_launch)
    monkeypatch.setattr(sm, "home_dir", lambda: tmp_path)

    class _FakeTimer:
        def __init__(self, delay, fn):
            calls["timers"].append(delay)

        def start(self):
            pass

    monkeypatch.setattr(threading, "Timer", _FakeTimer)
    monkeypatch.setattr(subprocess, "Popen",
                        lambda *a, **k: calls["popen"].append(a))
    monkeypatch.setattr(sys, "platform", "win32")
    return calls


def test_win_routes_to_windows_branch_not_systemd(win):
    import os
    _Stub()._hard_reboot("full")
    assert win["popen"] == []          # no systemd-run on Windows
    assert win["spawn"] is not None    # watcher armed instead
    assert win["spawn"]["old_pid"] == os.getpid()
    assert win["spawn"]["run_argv"][0] == sys.executable


def test_win_safe_mode_writes_boot_flag(win, tmp_path):
    _Stub()._hard_reboot("safe")
    assert (tmp_path / "NEXT_BOOT_SAFE").exists()
    assert win["timers"] == [2.0]      # reply beat, then quit force


def test_win_full_mode_clears_boot_flag(win, tmp_path):
    (tmp_path / "NEXT_BOOT_SAFE").touch()
    _Stub()._hard_reboot("full")
    assert not (tmp_path / "NEXT_BOOT_SAFE").exists()


def test_win_watcher_spawn_failure_leaves_gateway_running(win, tmp_path):
    win["spawn_ok"] = False
    _Stub()._hard_reboot("safe")
    assert win["timers"] == []                          # no self-kill scheduled
    assert not (tmp_path / "NEXT_BOOT_SAFE").exists()   # no half-armed state


def test_posix_path_untouched(monkeypatch):
    """Linux/macOS keep the systemd-run executor verbatim."""
    calls = []
    monkeypatch.setattr(sys, "platform", "linux")
    import subprocess

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: calls.append(a[0]))
    _Stub()._hard_reboot("safe")
    assert calls and calls[0][:2] == ["systemd-run", "--user"]

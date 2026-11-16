"""DEAF-STALL must not charge stale receive pulses to a gateway that predates them.

16.11 incident: the host slept ~15.7 h, Matrix sync froze; after resume + two host
reboots the watchdog restarted a 17 s old gateway on an inherited ``matrix=56664``
pulse — the persisted streak and pulse ages belonged to the previous process
generation. The sensor now grants boot grace: on a heartbeat-PID change (new
generation) and while the gateway is younger than DEAF_STALE_SECONDS, no deaf
streak accumulates and no restart fires.

Driven as a subprocess with the documented HERMES_GATEWAY_WD_* overrides and
--dry-run, so nothing touches systemd.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "gateway-watchdog.py"

STALE_PULSES = json.dumps({"matrix": 56664.24})


def _tick(tmp_path: Path, *, boot_age: str, pid: int, state: Path, log: Path) -> None:
    env = dict(
        os.environ,
        HERMES_HOME=str(tmp_path),
        HERMES_GATEWAY_WD_ACTIVE="active",
        HERMES_GATEWAY_WD_NR="0",
        HERMES_GATEWAY_WD_HB_AGE="5",
        HERMES_GATEWAY_WD_PULSES=STALE_PULSES,
        HERMES_GATEWAY_WD_BOOT_AGE=boot_age,
        HERMES_GATEWAY_WD_STATE=str(state),
        HERMES_GATEWAY_WD_LOG=str(log),
        WATCHDOG_HANG_ACTION="1",
    )
    subprocess.run([sys.executable, str(SCRIPT), "--dry-run"], env=env,
                   check=True, capture_output=True, timeout=30)


def _state(state: Path) -> dict:
    return json.loads(state.read_text())


def test_boot_grace_blocks_deaf_restart(tmp_path):
    """A freshly booted gateway with inherited stale pulses is never restarted."""
    state, log = tmp_path / "state.json", tmp_path / "wd.log"
    for _ in range(5):
        _tick(tmp_path, boot_age="10", pid=4242, state=state, log=log)
    text = log.read_text() if log.exists() else ""  # no log file = zero decisions
    assert "deaf-stall matrix=" not in text
    assert _state(state)["deaf_streaks"] == {"matrix": 0} or _state(state)["deaf_streaks"] == {}


def test_old_generation_stale_pulses_still_fire(tmp_path):
    """Control: an old-enough gateway with the SAME pid does confirm deafness."""
    state, log = tmp_path / "state.json", tmp_path / "wd.log"
    for _ in range(4):  # tick 1 = generation claim (grace), ticks 2-4 = streak 1..3
        _tick(tmp_path, boot_age="100000", pid=4242, state=state, log=log)
    text = log.read_text()
    assert "deaf-stall matrix=56664.24" in text
    assert "WOULD RESTART" in text.upper() or "would restart" in text


def test_pid_change_resets_persisted_streak(tmp_path):
    """A new heartbeat PID wipes a streak inherited from the previous life."""
    state, log = tmp_path / "state.json", tmp_path / "wd.log"
    state.write_text(json.dumps({
        "samples": [], "last_action_ts": 0.0, "hb_streak": 0,
        "hb_unknown_since": 0.0, "deaf_streaks": {"matrix": 2}, "pulse_pid": 111,
    }))
    (tmp_path / "gateway-heartbeat").write_text(
        f"222 {__import__('time').time()} {STALE_PULSES}\n")
    _tick(tmp_path, boot_age="100000", pid=4242, state=state, log=log)
    text = log.read_text()
    assert "new gateway generation pid=222" in text
    assert "deaf-stall matrix=" not in text.replace("new gateway generation", "")
    assert _state(state)["deaf_streaks"] == {}

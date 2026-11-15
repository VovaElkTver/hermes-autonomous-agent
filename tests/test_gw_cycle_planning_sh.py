import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "gw_cycle_planning.sh"

# Regression for the 15.11 orphan-deafness incident: a stop+start chain executed
# from inside hermes-gateway.service dies at the stop half (cgroup kill sweeps
# the executor), leaving the gateway down with StartUnit never issued.

CG_INSIDE = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/hermes-gateway.service\n"
CG_OUTSIDE = "0::/user.slice/user-1000.slice/user@1000.service/session-2.scope\n"


def _run(tmp_path: Path, cgroup: str, extra_env=None, args=()):
    events = tmp_path / "events.jsonl"
    events.write_text("")
    cgfile = tmp_path / "cgroup.txt"
    cgfile.write_text(cgroup)
    env = dict(
        os.environ,
        GW_CYCLE_EVENTS=str(events),
        GW_CYCLE_LOG=str(tmp_path / "activation.log"),
        GW_CYCLE_CGROUP_FILE=str(cgfile),
        GW_CYCLE_DRYRUN="1",
    )
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(SCRIPT), *args], capture_output=True, text=True,
        env=env, timeout=120, check=False,
    )


def test_script_is_valid_bash():
    r = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr


def test_inside_gateway_cgroup_rearms_instead_of_half_cycle(tmp_path):
    r = _run(tmp_path, CG_INSIDE)
    assert r.returncode == 0
    assert "would re-arm via systemd-run transient" in r.stdout
    # The stop/start must never be executed by a process inside the doomed cgroup.
    assert "systemctl --user stop" not in r.stdout.replace("would re-arm", "")


def test_armed_or_outside_executes_the_cycle(tmp_path):
    r = _run(tmp_path, CG_OUTSIDE, extra_env={"ARMED_VIA_SYSTEMD_RUN": "1"})
    assert r.returncode == 0
    assert "[dryrun] would: systemctl --user stop" in r.stdout


def test_selfcheck_dryrun_does_not_touch_systemd(tmp_path):
    r = _run(tmp_path, CG_INSIDE, args=("--selfcheck",))
    assert r.returncode == 0
    assert "[dryrun] selfcheck-out=" in r.stdout

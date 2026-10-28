"""Pump-CLI single-flight tests (audit 2026-10-28 item 5).

pump_cli must reach run_cycle through the SAME pump_lock as the ticker path:
a live foreign holder -> status «held», zero drain; a free lock -> the drain
runs under a stamped holder and releases it."""
import fcntl
import json
import os
import time


def _setup(tmp_path, monkeypatch):
    from controller import model, pump_lock
    real = model.connect
    monkeypatch.setattr(model, "connect", lambda db_path=None: real(tmp_path / "state.db"))
    monkeypatch.setattr(pump_lock, "default_lock_path", lambda: tmp_path / "pump-tick.lock")
    return real


def test_pump_cli_defers_to_a_live_holder(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)
    import controller.loop as loop_mod
    called = []
    monkeypatch.setattr(loop_mod, "run_cycle", lambda *a, **k: called.append(a))

    fh = open(tmp_path / "pump-tick.lock", "a+b")
    fcntl.flock(fh, fcntl.LOCK_EX)
    now = time.time()
    fh.write(json.dumps({"pid": os.getpid(), "pgid": os.getpgid(0),
                         "started_at": now, "last_active": now}).encode())
    fh.flush()
    try:
        from controller import pump_cli
        rc = pump_cli.main([])
        out = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert out["status"] == "held"  # a fake holder of a LIVE process
        assert called == []  # no drain happened at all
    finally:
        fh.close()


def test_pump_cli_drains_under_the_lock(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)
    import controller.loop as loop_mod
    from controller import model
    seen = {}

    def fake_run_cycle(conn, *, scope, worker_fn, review_fn, max_steps):
        raw = (tmp_path / "pump-tick.lock").read_bytes()
        seen["holder_pid"] = json.loads(raw or b"{}").get("pid")
        return [{"stop": "fake-drain"}]

    monkeypatch.setattr(loop_mod, "run_cycle", fake_run_cycle)
    scope = str(tmp_path / "wd")
    conn = model.connect()
    try:
        conn.execute("INSERT INTO ctl_autonomy_permission(scope, enabled,"
                     " execution_epoch, updated_at) VALUES (?,1,0,?)",
                     (scope, time.time()))
        conn.commit()
    finally:
        conn.close()

    from controller import pump_cli
    rc = pump_cli.main(["--scope", scope])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert seen["holder_pid"] == os.getpid()  # the drain ran INSIDE the lock
    assert out[scope] == [{"stop": "fake-drain"}]
    assert (tmp_path / "pump-tick.lock").read_text() == ""  # record erased

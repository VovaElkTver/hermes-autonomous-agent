"""controller.ticker — the resident in-thread tick loop of the autonomy loop.

MCU-style scheduler living INSIDE the hermes-gateway process: daemon threads
started by the controller plugin (NOT systemd, NOT cron, NOT a separate
process — vovak 24.10 06:21: "это внутренний thread самого гермес"). The pump
drain runs via ``loop.run_cycle`` directly in the scheduler thread, which also
keeps the worker pgid registry (adopt open runs from state.db, reap on death);
the deterministic watchdog (pump staleness, machine-park repair) ticks on its
OWN thread with its own DB connection, so a drain wedged inside run_cycle is
still observed. Template alerts deduped in state.db. No LLM composes status
text anywhere in this path.

Interval table (monotonic clock, re-anchored after any late tick):
  pump      5 min — drain every workdir that has ready/running tasks
  watchdog  30 min — pump staleness + machine-park repair (the rules of the
                     deleted human-gate-watchdog cron, now deterministic code)

Single-flight: the gateway process is the singleton; the pump tick still takes
the shared ``controller.pump_lock`` so a manual ``controller.pump_cli`` run or
the residual cron backup tick (24h parallel soak) cannot pile a second drain
on top of the in-thread one — a contended lock skips, never blocks.
Process ownership of the ticker itself is a plain flock
(``~/.hermes/run/controller-ticker.lock``): the kernel drops it when the
gateway dies, so the next gateway takes it without any stale-holder logic, and
a CLI/worker process that loads the plugin sees a live holder and does not
start a second ticker (its pump attempts are already skipped by the pump lock).

Workers stay tracked children: ``make_worker`` records pid/pgid on the run row
at spawn (controller.workers), so this thread's registry is DB-anchored —
``_adopt_workers`` registers open runs that carry a recorded worker, and
``_reap_workers`` reconciles a dead one (run -> interrupted, step claim
released) without ever needing a live process handle. A gateway restart finds
whatever survives via the same recorded pid/pgid — no orphans by construction.

Shutdown: the thread is a daemon; ``stop()`` joins it on SIGTERM (bounded,
mid-drain included), and an atexit hook makes a normal gateway exit join it
the same way. A hard kill cannot be joined — the restart-safe worker adoption
above is the fallback, by design.

Soak contract (task-ticker01): runs in parallel with the cron tick
b17bcdf067af for 24h; the heartbeats in ``ctl_ticker_heartbeat`` are the soak
evidence (kind='pump'/'watchdog'/'ticker-start'/'ticker-stop').
"""
from __future__ import annotations

import atexit
import fcntl
import json
import logging
import os
import threading
import time

from . import alerts, loop, model, pump_lock, queue as Q, transitions

logger = logging.getLogger("controller.ticker")

INTERVALS = {"pump": 300.0, "watchdog": 1800.0}
PUMP_STALE_S = 25 * 60   # the watchdog cron's own freshness rule
MACHINE_PARK_S = 30 * 60  # machine-parked longer than this is an incident


# ---- process ownership of the ticker (gateway singleton) ----------------------

_ownership_lock = threading.Lock()
_ownership_fd: int | None = None


def _ownership_path() -> os.PathLike[str]:
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "run" / "controller-ticker.lock"


def _gates_file_path() -> os.PathLike[str]:
    """The gate snapshot file the residual cron tick and gate tooling read
    (external contract kept fresh by the in-thread ticker during the soak)."""
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "run" / "pump-gates.json"


def _acquire_ownership() -> int | None:
    """Non-blocking exclusive flock: the kernel releases it when this process
    dies, so no stale-holder logic is needed. Returns an open fd we must keep
    for the thread's lifetime, or None when a live process owns the ticker."""
    global _ownership_fd
    with _ownership_lock:
        if _ownership_fd is not None:
            return _ownership_fd
        if os.name == "nt":  # no flock: start anyway — the pump lock still enforces single-flight
            _ownership_fd = -1
            return _ownership_fd
        path = _ownership_path()
        os.makedirs(os.path.dirname(str(path)), exist_ok=True)
        try:
            fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            logger.warning("ticker ownership lock open failed: %s", exc)
            _ownership_fd = -1
            return _ownership_fd
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return None
        _ownership_fd = fd
        return fd


def _release_ownership() -> None:
    global _ownership_fd
    with _ownership_lock:
        if _ownership_fd is None:
            return
        fd, _ownership_fd = _ownership_fd, None
    if fd >= 0:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass


# ---- the ticker ----------------------------------------------------------------


class Ticker:
    """Pump drain in this thread, watchdog on its OWN daemon thread with its own
    DB connection — a pump wedged inside ``loop.run_cycle`` never regains the
    thread, so a watchdog sharing it could not see the silence (audit 28.10)."""

    def __init__(self, *, intervals: dict | None = None, alert_target: str = "",
                 deliver_fn=None, now_fn=time.time, worker_model: str | None = None,
                 worker_provider: str | None = None, max_steps: int = 50,
                 gates_path: str | None = None):
        self.intervals = {**INTERVALS, **(intervals or {})}
        self.alert_target = alert_target
        self.deliver_fn = deliver_fn
        self._now = now_fn
        self.worker_model = worker_model
        self.worker_provider = worker_provider
        self.max_steps = max_steps
        self.gates_path = gates_path
        self._registry: dict[str, dict] = {}   # run_id -> {pid, pgid, task_id, adopted}
        self._prev_gates: dict | None = None
        self._epochs: dict[str, tuple] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._watchdog_thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="controller-ticker",
                                            daemon=True)
            self._thread.start()

    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def stop(self, timeout_s: float = 10.0) -> None:
        self._stop.set()
        for th in (self._thread, self._watchdog_thread):
            if th is not None:
                th.join(timeout_s)

    def _run(self) -> None:
        owned = _acquire_ownership()
        if owned is None:
            logger.info("controller ticker: another live process owns the ticker; "
                        "this process stays out of the loop (pump lock stays the guard)")
            return
        conn = model.connect()
        self._heartbeat(conn, "ticker-start",
                        {"pid": os.getpid(), "intervals": self.intervals})
        self._start_watchdog()
        try:
            self._adopt_workers(conn)
            self._schedule(conn)
        finally:
            self._stop.set()
            self._heartbeat(conn, "ticker-stop", {"pid": os.getpid()})
            conn.close()
            _release_ownership()

    def _start_watchdog(self) -> None:
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_run, name="controller-ticker-watchdog",
            daemon=True)
        self._watchdog_thread.start()

    def _watchdog_run(self) -> None:
        """Watchdog cadence on its own thread + connection: the pump tick can
        wedge in run_cycle and never return; this thread still ticks, still
        sees the aging 'pump' heartbeat and alerts."""
        conn = model.connect()
        try:
            next_tick = time.monotonic() + self.intervals["watchdog"]
            while not self._stop.is_set():
                delay = next_tick - time.monotonic()
                if delay > 0:
                    self._stop.wait(delay)
                if self._stop.is_set():
                    break
                self._guard("watchdog", conn, self.run_watchdog)
                next_tick = (max(next_tick, time.monotonic())
                             + self.intervals["watchdog"])
        finally:
            conn.close()

    def _schedule(self, conn) -> None:
        """The pump tick: the interval table, re-anchored after any late tick
        (a slow drain never collapses the cadence into a busy loop)."""
        now = self._now()
        next_pump = now
        while not self._stop.is_set():
            now = self._now()
            if now >= next_pump:
                self._guard("pump", conn, self.run_pump)
                next_pump = max(next_pump, self._now()) + self.intervals["pump"]
            self._stop.wait(max(0.0, next_pump - self._now()))

    def _guard(self, kind: str, conn, fn) -> None:
        """One tick: any exception becomes a heartbeat + a deduped tick-error
        alert, never a dead thread (a dead thread is the 24.10 silent-emitter
        incident, in-thread edition)."""
        try:
            fn(conn)
        except Exception as exc:  # the loop MUST survive any tick
            self._tick_error(conn, kind, exc)

    def _tick_error(self, conn, kind: str, exc: Exception) -> None:
        self._heartbeat(conn, f"tick-error:{kind}",
                        {"error": str(exc)[:200]})
        line = alerts.maybe_alert(conn, f"tick-error:{kind}", "tick-error",
                                  kind=kind, error=str(exc)[:120])
        if line:
            self._journal_and_deliver(conn, line)
        logger.warning("controller ticker: %s tick failed: %s", kind, exc)

    # -- pump tick -------------------------------------------------------------

    def run_pump(self, conn) -> None:
        self._reap_workers(conn)
        self._adopt_workers(conn)
        self._check_epochs(conn)
        lock_path = str(pump_lock.default_lock_path())
        lock = pump_lock.acquire(lock_path, conn=conn)
        if lock["status"] == "held":
            self._heartbeat(conn, "pump",
                            {"skipped": "lock held", "holder_pid": lock["holder_pid"]})
            return
        out: dict[str, list] = {}
        try:
            for scope in self._open_scopes(conn):
                perm = model.get_permission(conn, scope)
                if not perm["enabled"]:
                    out[scope] = [{"stop": "permission off"}]
                    pump_lock.heartbeat(lock_path)
                    continue
                out[scope] = loop.run_cycle(
                    conn, scope=scope, max_steps=self.max_steps,
                    worker_fn=self._worker_fn(conn),
                    review_fn=self._review_fn(conn))
                pump_lock.heartbeat(lock_path)
            self._heartbeat(conn, "pump",
                            {"scopes": {s: len(v) for s, v in out.items()}})
        finally:
            pump_lock.release(lock["handle"])
        # gate digest: human-facing lines only on gate SET change (state.db dedup)
        snap = alerts.render_gates_snapshot(conn)
        lines = alerts.gate_change_lines(conn, snap, self._prev_gates)
        self._prev_gates = snap
        self._write_gates_file(snap)
        for line in lines:
            self._journal_and_deliver(conn, line)

    def _open_scopes(self, conn) -> list[str]:
        return [r["workdir"] for r in conn.execute(
            "SELECT DISTINCT workdir FROM ctl_task"
            " WHERE state IN ('ready','running')").fetchall()]

    def _worker_cfg(self) -> tuple:
        """Fresh config read per drain (the cron tick did the same); a failed
        load degrades to default routing, never a crash."""
        try:
            from hermes_cli.config import load_config_readonly
            cfg = load_config_readonly() or {}
        except (ImportError, OSError):
            cfg = {}
        ctl = cfg.get("controller", {}) or {}
        return (ctl.get("worker_model") or self.worker_model,
                ctl.get("worker_provider") or self.worker_provider)

    def _worker_fn(self, conn):
        from controller.workers import make_worker
        lock_path = str(pump_lock.default_lock_path())
        model_id, provider = self._worker_cfg()
        return make_worker(
            conn,
            model_id=model_id, provider=provider,
            on_poll=lambda: pump_lock.heartbeat(lock_path))

    def _review_fn(self, conn):
        from controller.workers import make_review
        judge_cfg = None
        try:
            from hermes_cli.config import load_config_readonly
            cfg = load_config_readonly() or {}
            judge_cfg = (cfg.get("plugins", {}).get("entries", {})
                         .get("supervisor", {}).get("settings", {}).get("judge"))
        except (ImportError, OSError):
            pass
        return make_review(conn, judge_cfg=judge_cfg)

    def _write_gates_file(self, snap: dict) -> None:
        """Keep ~/.hermes/run/pump-gates.json fresh — the external contract the
        residual cron tick and gate tooling read during the parallel soak."""
        if not self.gates_path:
            return
        try:
            os.makedirs(os.path.dirname(self.gates_path), exist_ok=True)
            with open(self.gates_path, "w", encoding="utf-8") as fh:
                json.dump({"digest": alerts._digest(
                    json.dumps(snap, sort_keys=True, ensure_ascii=False)),
                    "ts": self._now(), "gates": snap}, fh, ensure_ascii=False)
        except OSError:
            pass

    # -- epoch re-check (per tick) ----------------------------------------------

    def _check_epochs(self, conn) -> None:
        """Re-read the autonomy permission for every scope each tick (a fresh
        epoch must cut the drain, and a grant/epoch change is a state change
        worth a journal line — not a chat line: the operator made it)."""
        cur: dict[str, tuple] = {}
        for r in conn.execute("SELECT scope, enabled, execution_epoch"
                              " FROM ctl_autonomy_permission").fetchall():
            cur[r["scope"]] = (bool(r["enabled"]), r["execution_epoch"])
        for scope, val in cur.items():
            if self._epochs.get(scope) != val:
                model.emit(conn, kind="ticker-permission-change",
                           origin="controller.ticker",
                           payload={"scope": scope, "now": val,
                                    "prev": self._epochs.get(scope)})
                self._epochs[scope] = val
        for scope in list(self._epochs):
            if scope not in cur:
                del self._epochs[scope]

    # -- watchdog (the deleted cron's rules, deterministic) ----------------------

    def run_watchdog(self, conn) -> None:
        # 1) pump staleness (watchdog rule 2): no 'pump' heartbeat within the
        #    freshness window => the tick went quiet. The watchdog runs on its
        #    own thread, so a drain wedged inside run_cycle (which never reaches
        #    _schedule again) is still caught here by heartbeat age.
        last = conn.execute("SELECT MAX(ts) m FROM ctl_ticker_heartbeat"
                            " WHERE kind='pump'").fetchone()["m"]
        if last is not None:
            age = self._now() - last
            if age > PUMP_STALE_S:
                line = alerts.maybe_alert(conn, "pump-stale", "pump-stale",
                                          age_min=int(age // 60))
                if line:
                    self._journal_and_deliver(conn, line)
        # 2) machine-park repair (watchdog rules 3+4b): a needs_human task
        #    parked on a 'verify:' basis that is NOT a revise question is
        #    machine-owned; all steps done + passing reviews => requeue.
        cands = self._machine_park_candidates(conn)
        live_keys = set()
        for c in cands:
            live_keys.add(f"machine-park:{c['task_id']}")
            if c["parked_for"] <= MACHINE_PARK_S:
                continue
            if not self._repair_eligible(conn, c["task_id"]):
                continue
            repaired = self._attempt_requeue(conn, c)
            if repaired:
                continue
            line = alerts.maybe_alert(conn, f"machine-park:{c['task_id']}",
                                      "machine-park", task_id=c["task_id"],
                                      basis=(c["basis"] or "")[:80])
            if line:
                self._journal_and_deliver(conn, line)
        # a cleared condition forgets its digest so the next park re-alerts
        for key in list(self._known_alert_keys(conn, "machine-park:")):
            if key not in live_keys:
                alerts.clear_alert(conn, key)
        self._heartbeat(conn, "watchdog",
                        {"machine_park": [c["task_id"] for c in cands]})

    def _machine_park_candidates(self, conn) -> list[dict]:
        out = []
        for t in conn.execute("SELECT task_id, workdir FROM ctl_task"
                              " WHERE state='needs_human'").fetchall():
            row = conn.execute(
                "SELECT basis, ts FROM ctl_state_transition"
                " WHERE task_id=? AND to_state='needs_human'"
                " ORDER BY ts DESC LIMIT 1", (t["task_id"],)).fetchone()
            if row is None:
                continue
            basis = row["basis"] or ""
            if not basis.startswith("verify:"):
                continue  # human gate (b): a question, not a stuck mechanism
            if basis.startswith("verify: review verdict revise"):
                continue  # human gate (c): a real question to the gate owner
            out.append({"task_id": t["task_id"], "workdir": t["workdir"],
                        "basis": basis, "parked_for": self._now() - row["ts"]})
        return out

    @staticmethod
    def _repair_eligible(conn, task_id: str) -> bool:
        """Watchdog rule 4b's criteria, verbatim: all steps done AND the latest
        review on every reviewed step is pass."""
        undone = conn.execute(
            "SELECT COUNT(*) c FROM ctl_step WHERE task_id=? AND status!='done'",
            (task_id,)).fetchone()["c"]
        if undone:
            return False
        rows = conn.execute(
            "SELECT r.verdict FROM ctl_review r"
            " WHERE r.step_id IN (SELECT step_id FROM ctl_step WHERE task_id=?) "
            "AND r.created_at=(SELECT MAX(r2.created_at) FROM ctl_review r2"
            "                  WHERE r2.step_id=r.step_id)", (task_id,)).fetchall()
        return bool(rows) and all(r["verdict"] == "pass" for r in rows)

    def _attempt_requeue(self, conn, cand: dict) -> bool:
        try:
            perm = model.get_permission(conn, cand["workdir"])
            transitions.transition(
                conn, cand["task_id"], "ready",
                basis="ticker-repair: machine park, criteria met",
                expected_epoch=perm["execution_epoch"])
            model.emit(conn, kind="ticker-repair", origin="controller.ticker",
                       task_id=cand["task_id"],
                       payload={"basis": cand["basis"]})
            logger.info("controller ticker: requeued %s (machine park)",
                        cand["task_id"])
            return True
        except (ValueError, LookupError) as exc:
            model.emit(conn, kind="ticker-repair-failed",
                       origin="controller.ticker", task_id=cand["task_id"],
                       payload={"error": str(exc)})
            return False

    @staticmethod
    def _known_alert_keys(conn, prefix: str) -> list[str]:
        rows = conn.execute("SELECT alert_key FROM ctl_alert_state").fetchall()
        return [r["alert_key"] for r in rows if r["alert_key"].startswith(prefix)]

    # -- worker pgid registry ------------------------------------------------------

    def _adopt_workers(self, conn) -> None:
        """Register open runs that carry a recorded worker (spawn-time
        record_run_process) — including survivors of a gateway restart, which
        is the adoption path: the new thread learns of them from state.db and
        reconciles them on death."""
        rows = conn.execute(
            "SELECT run_id, worker_pid, worker_pgid, task_id, run_status"
            " FROM ctl_run WHERE worker_pid IS NOT NULL"
            " AND run_status IN ('starting','running','interrupt_requested','unknown')"
        ).fetchall()
        for r in rows:
            if r["run_id"] in self._registry:
                continue
            self._registry[r["run_id"]] = {
                "pid": r["worker_pid"], "pgid": r["worker_pgid"],
                "task_id": r["task_id"], "adopted": True}
            model.emit(conn, kind="worker-adopted", origin="controller.ticker",
                       task_id=r["task_id"],
                       payload={"run_id": r["run_id"], "pid": r["worker_pid"]},
                       idempotency_key=f"adopt:{r['run_id']}:{r['worker_pid']}")

    def _reap_workers(self, conn) -> None:
        """Dead tracked worker -> run 'interrupted', step claim released,
        registry entry dropped. A live worker stays registered; the in-thread
        drain reaps its own children through worker_fn (communicate), this
        path covers adopted survivors only."""
        for run_id in list(self._registry):
            info = self._registry[run_id]
            if pump_lock._pid_liveness(info["pid"]) != "dead":
                continue
            row = conn.execute("SELECT run_status, step_id, task_id FROM ctl_run"
                               " WHERE run_id=?", (run_id,)).fetchone()
            if row is not None and row["run_status"] in Q.OPEN_RUN_STATUSES:
                model.set_run_status(conn, run_id, "interrupted")
                if row["step_id"]:
                    step = model.get_step(conn, row["step_id"])
                    if step is not None and step["status"] == "in_progress":
                        model.unstick_step(conn, row["step_id"])
                model.emit(conn, kind="worker-reaped", origin="controller.ticker",
                           task_id=row["task_id"],
                           payload={"run_id": run_id, "pid": info["pid"]})
            del self._registry[run_id]

    def registry(self) -> dict:
        """Thread-state view (acceptance: pgid registry in thread state)."""
        return {k: dict(v) for k, v in self._registry.items()}

    # -- delivery / journaling ------------------------------------------------------

    def _journal_and_deliver(self, conn, line: str) -> None:
        model.emit(conn, kind="alert", origin="controller.ticker",
                   payload={"line": line})
        if self.deliver_fn is not None:
            ok = self.deliver_fn(line)
        else:
            ok = alerts.deliver(line, self.alert_target)
        if not ok:
            logger.info("controller ticker: alert recorded in state.db, "
                        "chat delivery absent (no target or hermes send failed)")

    def _heartbeat(self, conn, kind: str, payload: dict) -> None:
        conn.execute(
            "INSERT INTO ctl_ticker_heartbeat(ts, kind, payload)"
            " VALUES (?,?,?)",
            (self._now(), kind, json.dumps(payload, ensure_ascii=False)))
        conn.commit()


# ---- process-wide singleton -----------------------------------------------------

_default: Ticker | None = None
_default_lock = threading.Lock()
_atexit_registered = False


def _load_controller_cfg() -> dict:
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly() or {}
    except (ImportError, OSError):
        cfg = {}
    return cfg.get("controller", {}) or {}


def start_default() -> Ticker | None:
    """Start the gateway-internal ticker (called from the plugin's register()).
    Returns the running Ticker, or None when this process must not run it."""
    global _default, _atexit_registered
    with _default_lock:
        if _default is not None and _default.alive():
            return _default
        ctl = _load_controller_cfg()
        t = Ticker(
            alert_target=str(ctl.get("alert_target") or ""),
            worker_model=ctl.get("worker_model"),
            worker_provider=ctl.get("worker_provider"),
            gates_path=str(_gates_file_path()),
        )
        if _acquire_ownership() is None:
            logger.info("controller ticker: another live gateway owns the loop; "
                        "not starting a second one")
            return None
        _default = t
        if not _atexit_registered:
            atexit.register(stop_default)
            _atexit_registered = True
        t.start()
        return t


def stop_default(timeout_s: float = 10.0) -> None:
    with _default_lock:
        if _default is not None:
            _default.stop(timeout_s)

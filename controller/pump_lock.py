"""controller.pump_lock — the pump tick's single-flight lock, with stale-holder takeover.

The tick holds this lock for the whole drain. A bare ``flock -n || exit 0`` has no
takeover: a wedged or dead holder silences every later tick, and the silence is the
failure. The 24.10 incident was exactly that — the pump died on an illegal-transition
ValueError and the lock stayed held for 13 hours; every 5-minute tick just exited,
and the queue sat dead with no signal.

The holder stamps its own record into the lock file (pid, pgid, started_at,
last_active) on acquisition and refreshes last_active as the drain progresses
(:func:`heartbeat`). A contender then decides from holder LIVINESS:

* no contention -> ``acquired``;
* holder record present, pid alive, heartbeat fresh -> ``held``: a normal
  concurrent tick, back off silently;
* recorded holder provably dead -> the flock may still be held by a live child
  that inherited the lock fd (the kernel drops it only when every open file
  description closes). The live fd-holders are found by scanning /proc and
  tree-killed, the lock is retaken -> ``taken_over``;
* holder alive but last_active older than ``stale_s`` (wedged: hung worker wait,
  dead loop) -> STALE SUSPECT on the first sighting (alert only); the tree-kill
  lands on the SECOND consecutive sighting of the same stall record;
* the recorded holder pid equals OUR pid -> never killpg (that would SIGKILL
  this very process): skip + alert;

Every takeover is journaled (``ctl_event`` kind ``pump_lock_takeover`` + a
logger line), never silent. Indeterminate liveness (unreadable record, /proc
unavailable, unkillable pid) fails closed to ``held``: a wrong kill is worse
than a quiet tick. Windows degrades to no lock (the DB's per-step CAS is still
the per-task single-flight); the tick script there never had a flock.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_STALE_S = 7200.0  # 2x the longest observed marathon turn (3595.8s, 27.09) + margin
_TAKEOVER_REACQUIRE_S = 5.0
_POLL_S = 0.2
# Stalls seen once are suspects, not corpses: a kill needs a SECOND consecutive
# sighting of the same stall record (first tick only alerts). Process-local by
# design — the resident ticker is the intended executioner; a short-lived CLI
# contender reports suspects and defers.
_STALE_SUSPECTS: set[tuple] = set()


def default_lock_path() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()) / "run" / "pump-tick.lock"


# ---- holder record (lives in the lock file itself, like the state.db lock) ----

def _holder_record(handle) -> dict | None:
    try:
        handle.seek(0)
        raw = handle.read(4096)
        rec = json.loads(raw.decode("utf-8", "replace")) if raw else None
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return rec if isinstance(rec, dict) else None


def _rewrite_lock(handle, payload: bytes) -> None:
    with contextlib.suppress(OSError, ValueError):
        handle.seek(0)
        handle.truncate()
        if payload:
            handle.write(payload)
        handle.flush()


def _stamp_holder(handle) -> None:
    try:
        pgid = os.getpgid(0)
    except (OSError, AttributeError):
        pgid = None
    now = time.time()
    _rewrite_lock(handle, json.dumps(
        {"pid": os.getpid(), "pgid": pgid, "started_at": now, "last_active": now},
        sort_keys=True).encode("utf-8"))


def _holder_pid(rec) -> int | None:
    if not rec:
        return None
    try:
        pid = int(rec["pid"])
    except (KeyError, TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def _record_stale(rec: dict, now: float, stale_s: float) -> bool:
    last = rec.get("last_active")
    return isinstance(last, (int, float)) and now - last > stale_s


def heartbeat(lock_path) -> None:
    """Refresh ``last_active`` for OUR record only. Never clobbers a foreign
    holder's record — a lock we don't hold is not ours to touch. Best effort:
    a lost heartbeat just means a later, slower takeover decision."""
    try:
        with open(lock_path, "r+b") as fh:
            rec = _holder_record(fh)
            if not rec or rec.get("pid") != os.getpid():
                return
            rec["last_active"] = time.time()
            _rewrite_lock(fh, json.dumps(rec, sort_keys=True).encode("utf-8"))
    except OSError:
        pass


# ---- holder liveness ---------------------------------------------------------

def _pid_liveness(pid) -> str:
    """'alive' | 'dead' | 'unknown'. Fail-closed: 'unknown' is treated as alive
    (a PermissionError means the pid exists under another uid; an OSError means
    we cannot prove otherwise)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return "unknown"
    if pid <= 0:
        return "unknown"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "dead"
    except PermissionError:
        return "alive"
    except OSError:
        return "unknown"
    return "alive"


def lock_file_holder_pids(lock_path) -> list[int]:
    """Pids of LIVE processes with an open fd on the lock file's inode.

    flock rides the open file description and fork() duplicates it: a holder
    that spawned children and died leaves the lock held by descendants that no
    longer appear in the holder record. A /proc/<pid>/fd scan is the only way
    to find them. Linux-only; ``[]`` off Linux or on any failure (callers fail
    closed). The caller's own fd is included — the caller excludes itself."""
    try:
        st = os.stat(lock_path)
        want = (st.st_dev, st.st_ino)
    except OSError:
        return []
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return []
    holders = []
    for pid_s in pids:
        fd_dir = f"/proc/{pid_s}/fd"
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            try:
                st2 = os.stat(f"{fd_dir}/{fd}")
            except OSError:
                continue
            if (st2.st_dev, st2.st_ino) == want:
                holders.append(int(pid_s))
                break
    return holders


def _process_age_s(pid: int) -> float | None:
    """Seconds since *pid* started; None when unknowable (off Linux, gone)."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            start_ticks = int(fh.read().rsplit(b")", 1)[1].split()[19])
        with open("/proc/uptime", "rb") as fh:
            uptime_s = float(fh.read().split()[0])
        return max(0.0, uptime_s - start_ticks / os.sysconf("SC_CLK_TCK"))
    except (OSError, ValueError, IndexError):
        return None


# ---- takeover ----------------------------------------------------------------


def _stale_reason(rec, lock_path, now: float, stale_s: float) -> str | None:
    """Why the contended lock is a STALL — or None when the holder is legitimate.
    None is fail-closed: we never kill on a guess."""
    pid = _holder_pid(rec)
    if rec is None:
        for hp in lock_file_holder_pids(lock_path):
            age = _process_age_s(hp) if hp != os.getpid() else 0.0
            if hp != os.getpid() and (age or 0) > stale_s:
                return (f"no holder record; fd-holder pid {hp} alive for "
                        f"{int(age or 0)}s (> {int(stale_s)}s)")
        return None
    if _pid_liveness(pid) == "dead":
        return f"recorded holder pid {pid} is dead; a live child may hold the lock fd"
    if _record_stale(rec, now, stale_s):
        return (f"holder pid {pid} alive but silent for "
                f"{int(now - rec['last_active'])}s (> {int(stale_s)}s)")
    return None


def _kill_stall_holders(lock_path, rec, stale_s: float) -> bool:
    """Tree-kill the stale lock's holders; True when at least one signal landed.

    A provably-dead recorded holder orphans the lock to its live fd-holders —
    those are killed without the age check: they cannot be healthy pumps (a
    healthy pump stamps its own fresh record). In every other case only
    fd-holders OLDER than stale_s are killed: a young fd-holder may be an
    in-progress contender or a fresh drain, and killing one is never justified.
    """
    from controller.workers import kill_worker_tree
    pid = _holder_pid(rec)
    now = time.time()
    dead_holder = rec is not None and _pid_liveness(pid) == "dead"
    killed = False
    if rec is not None and _pid_liveness(pid) == "alive" and _record_stale(rec, now, stale_s):
        killed = killed or kill_worker_tree(pid, rec.get("pgid"))
    for hp in lock_file_holder_pids(lock_path):
        if hp == os.getpid():
            continue
        if not dead_holder and (_process_age_s(hp) or 0) < stale_s:
            continue
        killed = killed or kill_worker_tree(hp, None)
    return killed


def _journal_takeover(conn, holder_pid, reason: str) -> None:
    """Durable journal of the takeover (best effort — the kill already happened)."""
    if conn is None:
        return
    try:
        from controller import model
        model.emit(conn, kind="pump_lock_takeover", origin="controller",
                   payload={"holder_pid": holder_pid, "reason": reason,
                            "takeover_pid": os.getpid()})
    except Exception:
        logger.debug("pump lock takeover journaling failed", exc_info=True)


def _held(handle, holder_pid, detail: str) -> dict:
    try:
        handle.close()
    except OSError:
        pass
    return {"status": "held", "handle": None, "holder_pid": holder_pid, "detail": detail}


def acquire(lock_path, *, stale_s: float = DEFAULT_STALE_S, conn=None) -> dict:
    """Acquire the pump single-flight lock, taking over STALE holders.

    Returns ``{"status": "acquired"|"taken_over"|"held", "handle": file|None,
    "holder_pid": int|None, "detail": str}``. For ``acquired``/``taken_over``
    ``handle`` is open and carries the flock — keep the process alive while
    draining (call :func:`heartbeat` periodically) and finish with
    :func:`release`. ``held`` means a live, recent holder kept the lock: back
    off, this tick does nothing."""
    if os.name == "nt":
        return {"status": "acquired", "handle": None, "holder_pid": os.getpid(),
                "detail": "no lock on Windows (per-task single-flight stays the DB CAS)"}
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+b")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        return _take_or_defer(handle, lock_path, stale_s, conn)
    _stamp_holder(handle)
    return {"status": "acquired", "handle": handle, "holder_pid": os.getpid(), "detail": ""}


def _sighting_key(rec, lock_path) -> tuple:
    """Identity of a stall, stable across consecutive sightings: the holder's
    (pid, last_active); with no record, the live fd-holder set minus myself."""
    if rec is not None:
        return (_holder_pid(rec), rec.get("last_active"))
    return ("fd-holders", tuple(sorted(set(lock_file_holder_pids(lock_path))
                                       - {os.getpid()})))


def _alert_stale(conn, phase: str, holder_pid, rec, now: float) -> None:
    """Journal the takeover decision (dedup alert row + event line); reporting
    never raises — a decision path must not die on its own bookkeeping."""
    if conn is None:
        return
    try:
        from controller import alerts
        age = rec.get("last_active", now) if rec else now
        line = alerts.maybe_alert(conn, f"pump-lock-stale:{holder_pid}:{phase}",
                                  "pump-lock-stale", pid=holder_pid, phase=phase,
                                  age_s=int(now - age))
        if line:
            from controller import model
            model.emit(conn, kind="pump_lock_stale_suspect", origin="controller",
                       payload={"phase": phase, "holder_pid": holder_pid})
    except Exception:
        logger.debug("pump lock stale alerting failed", exc_info=True)


def _take_or_defer(handle, lock_path: Path, stale_s: float, conn) -> dict:
    """Lock is contended: decide on the holder's liveness, and kill only stalls."""
    rec = _holder_record(handle)
    now = time.time()
    reason = _stale_reason(rec, lock_path, now, stale_s)
    if reason is None:
        return _held(handle, _holder_pid(rec), "holder alive and recent"
                     if rec else "no stale-holder evidence")
    holder_pid = _holder_pid(rec)
    if holder_pid is not None and holder_pid == os.getpid():
        _alert_stale(conn, "self-hold", holder_pid, rec, now)
        logger.warning("pump lock %s: stale holder is this process (pid %d) — "
                       "skip, NEVER self-kill (%s)", lock_path, holder_pid, reason)
        return _held(handle, holder_pid, f"self-held, no self-kill: {reason}")
    key = _sighting_key(rec, lock_path)
    if key not in _STALE_SUSPECTS:
        _STALE_SUSPECTS.add(key)
        _alert_stale(conn, "stale-suspect", holder_pid, rec, now)
        logger.warning("pump lock %s: %s — first sighting, kill deferred to "
                       "the next one", lock_path, reason)
        return _held(handle, holder_pid, f"stale-suspect (kill on second "
                                         f"sighting): {reason}")
    _STALE_SUSPECTS.discard(key)
    if not _kill_stall_holders(lock_path, rec, stale_s):
        return _held(handle, _holder_pid(rec),
                     "stale holder not killable (indeterminate liveness)")
    for _ in range(int(_TAKEOVER_REACQUIRE_S / _POLL_S)):
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except (BlockingIOError, OSError):
            time.sleep(_POLL_S)
    else:
        return _held(handle, _holder_pid(rec),
                     "stale holder killed but the lock was not released in time")
    _stamp_holder(handle)
    _journal_takeover(conn, _holder_pid(rec), reason)
    logger.warning("pump lock %s: %s; lock taken over by pid %d",
                   lock_path, reason, os.getpid())
    return {"status": "taken_over", "handle": handle, "holder_pid": _holder_pid(rec),
            "detail": f"takeover: {reason}"}


def release(handle) -> None:
    """Drop the lock and erase our holder record (a surviving record would look
    like an abnormal exit to the next contender). No-op when handle is None."""
    if handle is None:
        return
    _rewrite_lock(handle, b"")
    with contextlib.suppress(OSError):
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    with contextlib.suppress(OSError):
        handle.close()

"""Shared SQLite primitives for the small per-profile / board stores.

``open_db`` is the one connect + PRAGMA stack; ``transaction`` is the one commit-and-ALWAYS-close
shape. Every hand-rolled ``_connect``/``_transaction`` pair used to re-carry the #69567 fix (a
``with conn:`` only commits — it never closes, so each call leaked a connection and its WAL/SHM fds
until GC, exhausting ``RLIMIT_NOFILE`` on long-running gateways) and at least one copy missed it.
"""

from __future__ import annotations

import contextlib
import sqlite3
import time
from pathlib import Path
from typing import Callable, Iterator


def open_db(
    path: Path | str,
    *,
    db_label: str,
    busy_timeout_ms: int = 5000,
    wal: bool = True,
    foreign_keys: bool = False,
    synchronous_full: bool = False,
    row_factory=sqlite3.Row,
    check_same_thread: bool = True,
    wal_lock_retries: int = 1,
    initialize: Callable[[sqlite3.Connection], None] | None = None,
) -> sqlite3.Connection:
    """Open ``path`` (parent created), apply the PRAGMA set, run ``initialize``; closed if anything raises.

    ``busy_timeout_ms`` is the single busy knob: it is passed as ``connect(timeout=)`` AND set as the
    explicit PRAGMA so it is observable. ``wal=True`` goes through ``apply_wal_with_fallback`` — the
    only journal-mode setter that carries the WAL-reset-bug gate, the network-FS silent-refusal
    fallback and the never-live-downgrade invariant; a raw ``PRAGMA journal_mode=WAL`` bypasses all
    three. Only the transient ``database is locked`` from that pragma is retried (``wal_lock_retries``):
    a first opener initializing a shared DB can make it ignore the busy timeout, notably on Windows.
    """
    from hermes_state_wal import apply_wal_with_fallback

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Resolved at call time: fd-leak tests patch ``sqlite3.connect`` through the caller's module.
    conn = sqlite3.connect(path, timeout=busy_timeout_ms / 1000, check_same_thread=check_same_thread)
    try:
        conn.row_factory = row_factory
        conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        if wal:
            for attempt in range(wal_lock_retries):
                try:
                    apply_wal_with_fallback(conn, db_label=db_label)
                    break
                except sqlite3.OperationalError as exc:
                    if str(exc).lower() != "database is locked" or attempt + 1 == wal_lock_retries:
                        raise
                    time.sleep(0.01 * (2**attempt))
        if foreign_keys:
            conn.execute("PRAGMA foreign_keys=ON")
        if synchronous_full:
            conn.execute("PRAGMA synchronous=FULL")
        if initialize is not None:
            initialize(conn)
    except BaseException:
        conn.close()
        raise
    return conn


@contextlib.contextmanager
def transaction(conn: sqlite3.Connection, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
    """Commit on success, roll back on error, and ALWAYS close ``conn`` (see the module docstring)."""
    try:
        if immediate:
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc).lower() and _dblock_trace_budget():
                    _dump_dblock_stacks("transaction/BEGIN IMMEDIATE", exc)
                raise
        with conn:
            yield conn
    finally:
        conn.close()


_dblock_trace_left = [12]


def _dblock_trace_budget() -> bool:
    """Local probe: cap stack dumps so a lock storm cannot flood the log."""
    if _dblock_trace_left[0] <= 0:
        return False
    _dblock_trace_left[0] -= 1
    return True


_SHM_LOCK_NAMES = {120: "WRITER", 121: "CKPT", 122: "RECOVER", 123: "R0", 124: "R1",
                   125: "R2", 126: "R3", 127: "R4", 128: "R5"}


def _dump_kernel_locks(fh) -> None:
    """Kernel-truth map of byte locks held on the dev-home store sidecars (local probe)."""
    import fcntl as _fcntl
    import os as _os
    import struct as _struct
    from pathlib import Path as _P
    home = _P.home() / ".hermes-dev"
    _ofd_getlk = getattr(_fcntl, "F_OFD_GETLK", 37)  # Linux: value 37 when the constant is undefined
    for name in ("state.db-shm", "state.db-wal", "state.db"):
        path = home / name
        try:
            fd = _os.open(path, _os.O_RDWR)
        except OSError as ex3:
            fh.write(f"klock {name}: open-failed {ex3}\n")
            continue
        try:
            for offset in range(120, 133):
                request = _struct.pack("BBLQQ", _fcntl.F_WRLCK, 0, 0, offset, 1)
                res = _fcntl.fcntl(fd, _fcntl.F_GETLK, request)
                l_type, _, holder_pid, _start, length = _struct.unpack("BBLQQ", res)
                if l_type == _fcntl.F_UNLCK:
                    continue
                kind = "W" if l_type == _fcntl.F_WRLCK else "R"
                label = _SHM_LOCK_NAMES.get(offset, str(offset)) if name.endswith("-shm") else "?"
                fh.write(f"klock {name}: {kind} byte={offset}({label}) pid={holder_pid} len={length}\n")
        except Exception as ex2:
            fh.write(f"klock {name}: unavailable {ex2}\n")
        finally:
            _os.close(fd)


def _dump_dblock_stacks(where: str, exc: Exception) -> None:
    """Record every thread's stack when the store is locked by a long txn (local probe)."""
    import sys
    import traceback
    import types
    try:
        from pathlib import Path as _P
        log = _P.home() / ".hermes-dev" / "logs" / "dblock-trace.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(f"=== {time.strftime('%Y-%m-%d %H:%M:%S')} {where}: {exc}\n")
            try:
                import gc as _gc
                total = 0
                for obj in _gc.get_objects():
                    if not isinstance(obj, sqlite3.Connection):
                        continue
                    total += 1
                    # NOTE: sqlite3.Connection has no fileno(); guard each attribute separately so a
                    # single bad attribute cannot silently kill whole IN-TXN detection.
                    try:
                        itx = bool(obj.in_transaction)
                    except Exception:
                        continue
                    if not itx:
                        continue
                    iso = changes = None
                    try:
                        iso = obj.isolation_level
                    except Exception:
                        pass
                    try:
                        changes = obj.total_changes
                    except Exception:
                        pass
                    fh.write(f"!!! IN-TXN conn id={id(obj):#x} isolation={iso!r} changes={changes}\n")
                    try:
                        dbs = obj.execute("PRAGMA database_list").fetchall()
                        fh.write(f"    dbs={dbs}\n")
                    except Exception as dexc:
                        fh.write(f"    dbs-unavailable: {dexc}\n")
                    try:
                        import gc as _gc2
                        sigs = []
                        for ref in _gc2.get_referrers(obj)[:10]:
                            tn = type(ref).__name__
                            extra = ""
                            if isinstance(ref, dict):
                                extra = ",".join(str(k)[:24] for k in list(ref.keys())[:6])
                            elif isinstance(ref, types.FrameType):
                                extra = f"{ref.f_code.co_name}:{ref.f_lineno}"
                            elif hasattr(ref, "__dict__"):
                                extra = ",".join(list(getattr(ref, "__dict__", {}).keys())[:8])[:120]
                            owners = ""
                            paths = []
                            if isinstance(ref, dict):
                                for owner in _gc2.get_referrers(ref)[:4]:
                                    otn = type(owner).__name__
                                    if isinstance(owner, types.FrameType) or otn in ("dict", "list", "tuple", "set", "frozenset"):
                                        continue
                                    owners += f"<-{otn}"
                                    def _find(node_, prefix_="", depth_=0):
                                        found_ = []
                                        try:
                                            items_ = list(vars(node_).items())
                                        except Exception:
                                            return found_
                                        for k, v in items_:
                                            hit = (
                                                v is obj
                                                or (isinstance(v, (list, tuple, set, frozenset)) and any(x is obj for x in v))
                                                or (isinstance(v, dict) and any(x is obj for x in v.values())))
                                            if hit:
                                                found_.append(prefix_ + k)
                                            elif depth_ < 2 and not isinstance(v, (str, bytes, int, float, bool)):
                                                found_.extend(_find(v, prefix_ + k + ".", depth_ + 1))
                                        return found_
                                    names = _find(owner)
                                    if names:
                                        paths.append(f"{otn}({','.join(names[:4])})")
                            sigs.append(f"{tn}[{extra}]{owners}{'##' + ';'.join(paths) if paths else ''}")
                        fh.write("    refs: " + " | ".join(sigs) + "\n")
                    except Exception:
                        pass
                fh.write(f"conns_total={total}\n")
            except Exception:
                pass
            try:
                _dump_kernel_locks(fh)
            except Exception:
                pass
            for tid, frame in sys._current_frames().items():
                fh.write(f"--- thread {tid}\n")
                fh.write("".join(traceback.format_stack(frame))[-1800:])
    except Exception:
        pass


def add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> bool:
    """``ALTER TABLE <table> ADD COLUMN <ddl>``, idempotent across races: True when this call added
    it, False on the ``duplicate column name`` a concurrent migrator caused.

    ``column`` is the human-readable name for the call site; ``ddl`` carries the actual definition. See
    #21708.
    """
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
        return True
    except sqlite3.OperationalError as exc:
        if "duplicate column name" in str(exc).lower():
            return False
        raise


@contextlib.contextmanager
def write_txn(conn: sqlite3.Connection):
    """An IMMEDIATE write transaction on a long-lived connection (stays open). The explicit ROLLBACK is
    guarded so a SQLite auto-rollback (no transaction left under EIO / contention / corruption) cannot
    shadow the original error."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise
    else:
        conn.execute("COMMIT")

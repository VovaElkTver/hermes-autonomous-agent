"""M1 (TZ-memdoor-5090-v2): target-based step acceptance — mario mechanics, not mario.

A step may carry ``accept``: a machine check that PROVES the step's outcome.
The donor semantics we port from WORKFLOWS.md:8-10/:78-80:

* a step is done when its target holds — never because the model said so;
* the check is not the work: work happens in the turn, the accept command only
  verifies («Put the work in the prompt; put what proves it in the target»);
* an already-held target means SKIP (step-level resume for free: done steps are
  invisible to queue.next_step);
* target_guard: the files the accept command reads are snapshotted at dispatch
  and restored before evaluation — the agent cannot rewrite its own check
  mid-step (measured in the mario experiment: see ~/zoo/mario-exp/
  MARIO-EXPERIMENT.md).

Conscious limitation ported verbatim from the experiment (finding #1): there is
NO lineage invalidation — if an upstream step re-runs, downstream steps whose
own target still holds stay done. The accept is bound to THIS step's proof,
not to freshness against upstream. Reopening downstream is a contract decision,
not scheduler magic.

OFF by default (``target_accept.enabled`` / ``HERMES_TARGET_ACCEPT``): with the
flag off no snapshot, no evaluation, no events — ``finish_step`` behaves byte-
identically to before (accept columns stay written-but-unread, exactly the S1
journal discipline).
"""

from __future__ import annotations

import base64
import json
import os
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

_DEFAULT_EVAL_TIMEOUT = 30.0
_MAX_TIMEOUT = 120.0
_OUTPUT_TAIL = 1200  # chars of combined output kept as evidence


def target_accept_enabled() -> bool:
    """``target_accept.enabled`` (default **False**) / ``HERMES_TARGET_ACCEPT``."""
    env = os.environ.get("HERMES_TARGET_ACCEPT")
    if env is not None:
        return env.strip().lower() in ("1", "true", "yes", "on")
    try:
        from hermes_cli.config import load_config
        cfg = load_config() or {}
        val = (cfg.get("target_accept") or {}).get("enabled")
        return bool(val) if val is not None else False
    except Exception:
        return False


def parse_accept(raw: Any) -> dict | None:
    """Validate a step's accept spec. Returns None (no target) or a normalized
    dict ``{"command": ...}`` / ``{"file": ...}`` with optional ``timeout``.
    Raises ValueError on a malformed spec so enqueue fails LOUD, not silent."""
    if raw is None or raw == "" or raw == {}:
        return None
    if isinstance(raw, str):
        raw = {"command": raw}
    if not isinstance(raw, dict):
        raise ValueError(f"accept must be an object, got {raw!r}")
    cmd = str(raw.get("command") or "").strip()
    file_ = str(raw.get("file") or "").strip()
    if bool(cmd) == bool(file_):
        raise ValueError("accept needs exactly one of 'command' or 'file'")
    spec: dict = {"command": cmd} if cmd else {"file": file_}
    if raw.get("timeout") is not None:
        try:
            spec["timeout"] = min(float(raw["timeout"]), _MAX_TIMEOUT)
        except (TypeError, ValueError):
            raise ValueError(f"accept.timeout not numeric: {raw.get('timeout')!r}")
    return spec


def _snapshot_dir() -> Path:
    from hermes_constants import get_hermes_home
    d = get_hermes_home() / "cache" / "accept_snapshots"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _check_files(spec: dict, workdir: str) -> list[str]:
    """The programs the check reads: existing file-path tokens of the command
    (donor target_guard: snapshot/restore of the programs named in the target).
    A ``file`` target is itself one program — the file whose existence proves."""
    if "file" in spec:
        p = spec["file"]
        full = p if os.path.isabs(p) else os.path.join(workdir or ".", p)
        return [os.path.abspath(full)]
    found: list[str] = []
    try:
        toks = shlex.split(spec["command"])
    except ValueError:
        return []
    for tok in toks:
        if "/" not in tok and not tok.endswith((".sh", ".py", ".go", ".txt", ".json", ".md")):
            continue
        full = tok if os.path.isabs(tok) else os.path.join(workdir or ".", tok)
        if os.path.isfile(full):
            found.append(os.path.abspath(full))
    return sorted(set(found))


def snapshot_step(step_id: str, spec: dict, workdir: str) -> int:
    """Freeze the check's programs at dispatch time. Returns file count (0 =
    nothing to guard, e.g. the check creates its target file itself)."""
    files = _check_files(spec, workdir)
    blob: dict[str, str] = {}
    for f in files:
        try:
            blob[f] = base64.b64encode(Path(f).read_bytes()).decode("ascii")
        except OSError:
            continue
    (_snapshot_dir() / f"{step_id}.json").write_text(
        json.dumps({"files": blob, "ts": time.time()}), encoding="utf-8")
    return len(blob)


def _restore(step_id: str) -> list[str]:
    """Write frozen programs back when the agent touched them. Returns the
    tampered paths (empty when the snapshot is absent or untouched)."""
    p = _snapshot_dir() / f"{step_id}.json"
    if not p.is_file():
        return []
    tampered: list[str] = []
    try:
        blob = json.loads(p.read_text(encoding="utf-8")).get("files") or {}
    except (OSError, ValueError):
        return []
    for f, b64 in blob.items():
        want = base64.b64decode(b64)
        try:
            if Path(f).read_bytes() != want:
                Path(f).write_bytes(want)
                tampered.append(f)
        except OSError:
            continue
    return tampered


def holds(spec: dict, workdir: str, *, timeout: float | None = None) -> tuple[bool, dict]:
    """Evaluate a target WITHOUT restore (pre-dispatch skip check). Returns
    (holds, evidence). A crash/timeout of the checker is NOT a held target."""
    if "file" in spec:
        f = spec["file"]
        full = f if os.path.isabs(f) else os.path.join(workdir or ".", f)
        ok = os.path.exists(full)
        return ok, {"file": os.path.abspath(full), "exists": ok}
    t = float(spec.get("timeout") or timeout or _DEFAULT_EVAL_TIMEOUT)
    try:
        r = subprocess.run(["bash", "-c", spec["command"]], check=False,
                           cwd=workdir or None, capture_output=True, text=True,
                           timeout=min(t, _MAX_TIMEOUT))
        rc = r.returncode
        out = ((r.stdout or "") + (r.stderr or ""))[-_OUTPUT_TAIL:]
    except subprocess.TimeoutExpired:
        rc, out = -1, f"accept checker timed out (>{t:.0f}s)"
    except OSError as exc:
        rc, out = -1, f"accept checker failed to start: {exc}"
    return rc == 0, {"rc": rc, "output": out}


def _latest_run_id(conn, step_id: str) -> str | None:
    row = conn.execute(
        "SELECT run_id FROM ctl_run WHERE step_id=? ORDER BY rowid DESC LIMIT 1",
        (step_id,)).fetchone()
    return row["run_id"] if row else None


def _record(conn, step_id: str, payload: dict) -> None:
    """Receipt goes on the run row when one exists, else only into the event —
    a pre-dispatch skip has no run yet (the event IS its receipt)."""
    rid = _latest_run_id(conn, step_id)
    if rid is None:
        return
    conn.execute("UPDATE ctl_run SET accept_result=? WHERE run_id=?",
                 (json.dumps(payload, ensure_ascii=False), rid))


def try_close(conn, *, step_id: str, task_id: str, workdir: str,
              accept_raw: str, origin: str) -> dict:
    """The chokepoint: attempt status=done for a step that carries an accept.
    Restores the guard files, evaluates, and lets done stick only when the
    target holds. Never raises: a broken spec is a failed target, loudly."""
    from controller import model
    try:
        spec = parse_accept(json.loads(accept_raw))
    except (ValueError, TypeError) as exc:
        return {"ok": False, "stored": None, "rc": None,
                "error": f"accept spec unreadable: {exc}", "evidence": "",
                "tampered": []}
    if spec is None:
        return {"ok": True, "stored": None, "rc": None, "error": "",
                "evidence": "", "tampered": [], "no_target": True}
    tampered = _restore(step_id)
    ok, ev = holds(spec, workdir)
    ev["tampered"] = tampered
    model.emit(conn, kind="step_accept_pass" if ok else "step_accept_fail",
               origin=origin, task_id=task_id,
               payload={"step_id": step_id, **ev})
    _record(conn, step_id, {"holds": ok, "ts": time.time(), **ev})
    conn.commit()
    (_snapshot_dir() / f"{step_id}.json").unlink(missing_ok=True)
    if ok:
        return {"ok": True, "stored": "done", "rc": ev.get("rc"),
                "error": "", "evidence": "", "tampered": tampered}
    return {"ok": False, "stored": None, "rc": ev.get("rc"),
            "error": "target not held — the step's own check says no; fix the "
                     "work, not the checker (guard files are restored before "
                     "evaluation)",
            "evidence": ev.get("output", ""), "tampered": tampered}

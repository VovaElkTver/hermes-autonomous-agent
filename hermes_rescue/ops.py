"""The hands kit: the ONLY irreversible operations the rescue core owns.
Every op is parameterless-or-enumerated, journal-logged, dry-run aware.
No general shell: if it is not here, the rescue core cannot do it."""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from datetime import datetime, timezone, UTC
from pathlib import Path

from .config import RescueConfig


def _journal(cfg: RescueConfig, op: str, detail: dict) -> None:
    cfg.rescue_home.mkdir(parents=True, exist_ok=True)
    rec = {"ts": time.time(), "iso": datetime.now(UTC).isoformat(),
           "op": op, "dry_run": cfg.dry_run, **detail}
    with (cfg.rescue_home / "journal.jsonl").open("a") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _run(cmd: list[str], cfg: RescueConfig, timeout: int = 60) -> str:
    if cfg.dry_run:
        return f"<dry-run: {' '.join(cmd)}>"
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    return (p.stdout or p.stderr).strip()


def snapshot_db(cfg: RescueConfig) -> str:
    src = cfg.hermes_home / "state.db"
    dst_dir = cfg.rescue_home / "db-snapshots"
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / f"state-{time.strftime('%y%m%d-%H%M%S')}.db"
    if cfg.dry_run:
        _journal(cfg, "snapshot_db", {"src": str(src), "dst": str(dst), "skipped": True})
        return str(dst)
    # consistent copy of a live WAL database: online backup API, not cp.
    import sqlite3
    src_c = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=5)
    dst_c = sqlite3.connect(dst, timeout=10)
    try:
        src_c.backup(dst_c)
    finally:
        src_c.close()
        dst_c.close()
    _journal(cfg, "snapshot_db", {"src": str(src), "dst": str(dst),
                                  "size_mb": round(dst.stat().st_size / 1e6, 1)})
    return str(dst)


def switch_release(cfg: RescueConfig, release: str) -> str:
    """Atomically repoint ~/hermes-live -> releases/<release> and restart the unit."""
    target = cfg.releases_dir / release
    if not target.is_dir():
        _journal(cfg, "switch_release", {"release": release, "error": "no such release"})
        return f"refused: release '{release}' not in {cfg.releases_dir}"
    if not any((Path(p) / "gateway" / "run.py").exists() for p in [target]):
        _journal(cfg, "switch_release", {"release": release, "error": "not a kernel dir"})
        return f"refused: {target} is not an unpacked kernel"
    link = cfg.current_link
    prev = str(link.resolve()) if link.is_symlink() else "<none>"
    out = _run(["ln", "-sfn", str(target), str(link)], cfg)
    _run(["systemctl", "--user", "restart", cfg.main_unit], cfg)
    _journal(cfg, "switch_release", {"release": release, "prev": prev, "out": out[:200]})
    return f"switched {prev} -> {target}"


def restart_unit(cfg: RescueConfig) -> str:
    out = _run(["systemctl", "--user", "restart", cfg.main_unit], cfg)
    _journal(cfg, "restart_unit", {"out": out[:200]})
    return out or "restarted"


def lift_estop(cfg: RescueConfig) -> str:
    sent = cfg.hermes_home / "ESTOP"
    if sent.exists():
        if not cfg.dry_run:
            sent.unlink()
        _journal(cfg, "lift_estop", {"was": str(sent.read_text(errors='replace')[:120] if sent.exists() else "")})
        return "ESTOP lifted"
    return "ESTOP not engaged"


def observe(cfg: RescueConfig, seconds: int | None = None) -> dict:
    """Watch the unit for `seconds` after an action; healthy = stable MainPID."""
    from . import inspect as insp
    time.sleep(seconds or cfg.observe_after_action_s)
    props = insp._unit_state(cfg)
    healthy = props.get("ActiveState") == "active" and props.get("SubState") == "running"
    _journal(cfg, "observe", {"healthy": healthy, "props": props})
    return {"healthy": healthy, **props}


def escalate(cfg: RescueConfig, reason: str) -> str:
    _journal(cfg, "escalate", {"reason": reason})
    return f"escalated to human: {reason}"

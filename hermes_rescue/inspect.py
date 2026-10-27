"""Read-only inspection of the main node. Never raises: every probe degrades
to an error string in the dossier — the rescue core must survive seeing a
broken world."""
from __future__ import annotations

import json
import sqlite3
import subprocess
import time
from pathlib import Path

from .config import RescueConfig


def _sh(cmd: list[str], timeout: int = 10) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        return (p.stdout or p.stderr).strip()
    except Exception as exc:
        return f"<probe failed: {exc}>"


def _unit_state(cfg: RescueConfig) -> dict:
    out = _sh(["systemctl", "--user", "show", cfg.main_unit,
               "-p", "ActiveState", "-p", "SubState", "-p", "MainPID",
               "-p", "NRestarts", "-p", "ExecMainStartTimestamp", "--no-pager"])
    props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    return props


def _journal_tail(cfg: RescueConfig) -> str:
    return _sh(["journalctl", "--user", "-u", cfg.main_unit, "-n", "40",
                "--no-pager", "-o", "cat"])


def _db_probe(path: Path) -> dict:
    info: dict = {"path": str(path)}
    try:
        info["size_mb"] = round(path.stat().st_size / 1e6, 1)
    except OSError as exc:
        return {**info, "error": str(exc)}
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3)
        try:
            info["integrity"] = con.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            con.close()
    except Exception as exc:
        info["error"] = f"open/quick_check: {exc}"
    return info


def inspect_main(cfg: RescueConfig) -> dict:
    """Full dossier of the main node, JSON-serializable, read-only."""
    dossier: dict = {"ts": time.time()}
    dossier["unit"] = _unit_state(cfg)
    hb = cfg.hermes_home / "gateway-heartbeat"
    try:
        pid, epoch = hb.read_text().split()
        dossier["heartbeat"] = {"age_s": round(time.time() - float(epoch)), "pid": pid}
    except Exception as exc:
        dossier["heartbeat"] = {"error": str(exc)}
    dossier["estop"] = (cfg.hermes_home / "ESTOP").exists()
    dossier["state_db"] = _db_probe(cfg.hermes_home / "state.db")
    # code pointer: release model first, dev repo as a fallback fact
    if cfg.current_link.exists():
        dossier["live_code"] = {"current": str(Path(cfg.current_link).resolve()),
                                "releases": sorted(p.name for p in cfg.releases_dir.iterdir())
                                if cfg.releases_dir.exists() else []}
    else:
        head = _sh(["git", "-C", str(cfg.repo_dir), "rev-parse", "--short", "HEAD"])
        dirty = _sh(["git", "-C", str(cfg.repo_dir), "status", "--porcelain"]) != ""
        dossier["live_code"] = {"mode": "dev-repo (release model not installed)",
                                "head": head, "dirty_worktree": dirty}
    dossier["watchdog_tail"] = _tail_file(cfg.hermes_home / "logs" / "gateway-gateway-watchdog.log", 15)
    dossier["journal_tail"] = _journal_tail(cfg)
    return dossier


def _tail_file(path: Path, n: int) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-n:])
    except OSError as exc:
        return f"<unreadable: {exc}>"


def dossier_text(dossier: dict) -> str:
    return json.dumps(dossier, indent=1, ensure_ascii=False, default=str)

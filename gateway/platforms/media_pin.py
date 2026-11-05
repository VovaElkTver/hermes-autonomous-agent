"""Evidence pins: keep cited inbound media in the cache for re-verification.

2026-11-03 incident contract: the agent declared a fact about a screenshot, was
corrected, and could not re-check its own claim because the cached media file
had already been swept by the age-based cleanup. A pin records "this file is
cited evidence" so ``_cleanup_cache_dir`` skips it while the pin is alive.

Design constraints (kept deliberately small):
- pins live in ONE json sidecar under ``$HERMES_HOME/cache/media_pins.json``;
  atomic-replace writes;
- every prepared turn that references the media extends its pin (rolling TTL),
  bounded by an absolute cap anchored at the FIRST pin, so no file is immortal;
- an unknown/corrupt store degrades to "no pins", never blocks deletion.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

# Rolling window from the last turn that referenced the file ...
PIN_TTL_HOURS = 24.0
# ... bounded by an absolute cap from the first pin (no immortal files).
ABSOLUTE_PIN_HOURS = 24.0 * 7

_LOCK = threading.Lock()


def _env_enabled() -> bool:
    return os.environ.get("HERMES_MEDIA_PINS", "1").strip().lower() not in {"0", "false", "off", "no"}


def _store_path() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()) / "cache" / "media_pins.json"


def _load(path: Optional[Path] = None) -> dict[str, dict[str, float]]:
    try:
        raw = json.loads((path or _store_path()).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:
        logger.debug("media_pins.json unreadable; treating as empty", exc_info=True)
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, float]] = {}
    for key, val in raw.items():
        if isinstance(val, dict) and isinstance(val.get("until"), (int, float)) \
                and isinstance(val.get("born"), (int, float)):
            out[str(key)] = {"until": float(val["until"]), "born": float(val["born"])}
    return out


def _write(pins: dict[str, dict[str, float]], path: Optional[Path] = None) -> None:
    target = path or _store_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(pins, sort_keys=True), encoding="utf-8")
    os.replace(tmp, target)


def pin_media_file(path: str | os.PathLike[str], *, now_ts: Optional[float] = None,
                   ttl_hours: float = PIN_TTL_HOURS,
                   absolute_hours: float = ABSOLUTE_PIN_HOURS,
                   _path_override: Optional[Path] = None) -> bool:
    """Pin one cached-media path until ``now + ttl``, capped at ``first_pin + absolute``.

    Idempotent and safe against concurrent gateway turns; True when the store changed."""
    key = str(Path(path).resolve())
    now = now_ts if now_ts is not None else time.time()
    with _LOCK:
        pins = _load(_path_override)
        entry = pins.get(key)
        born = entry["born"] if entry else now
        until = min(now + ttl_hours * 3600.0, born + absolute_hours * 3600.0)
        if entry and entry["until"] == until:
            return False
        pins[key] = {"until": until, "born": born}
        try:
            _write(pins, _path_override)
        except OSError:
            logger.debug("Could not persist media pin for %s", key, exc_info=True)
            return False
    return True


def pin_media_files(paths: Iterable[str | os.PathLike[str]], **kwargs) -> int:
    """Pin many paths; paths not currently on disk are still pinned (caches move files)."""
    changed = 0
    for p in paths or []:
        if p:
            changed += 1 if pin_media_file(p, **kwargs) else 0
    return changed


def pinned_until(path: str | os.PathLike[str], *,
                 _path_override: Optional[Path] = None) -> Optional[float]:
    """Expiry ts of the live pin on *path*, or None when unpinned/expired."""
    key = str(Path(path).resolve())
    with _LOCK:
        entry = _load(_path_override).get(key)
    if not entry or entry["until"] < time.time():
        return None
    return entry["until"]


def prune_expired(*, now_ts: Optional[float] = None,
                  _path_override: Optional[Path] = None) -> int:
    """Drop expired entries and pins whose files no longer exist; returns removed count."""
    now = now_ts if now_ts is not None else time.time()
    with _LOCK:
        pins = _load(_path_override)
        keep: dict[str, dict[str, float]] = {}
        removed = 0
        for key, entry in pins.items():
            alive = entry["until"] >= now and os.path.exists(key)
            if alive:
                keep[key] = entry
            else:
                removed += 1
        if len(keep) != len(pins):
            try:
                _write(keep, _path_override)
            except OSError:
                logger.debug("Could not rewrite media_pins.json during prune", exc_info=True)
    return removed

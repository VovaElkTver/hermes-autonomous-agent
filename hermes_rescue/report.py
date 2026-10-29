"""Report delivery over bare Matrix REST — no adapter, no gateway import."""
from __future__ import annotations

import logging
import time

import httpx

from .config import RescueConfig

log = logging.getLogger("hermes_rescue.report")


def send(cfg: RescueConfig, text: str) -> bool:
    if not (cfg.homeserver and cfg.access_token and cfg.home_room):
        log.warning("report: matrix not configured, text follows on stdout:\n%s", text)
        return False
    url = f"{cfg.homeserver}/_matrix/client/v3/rooms/{cfg.home_room}/send/m.room.message/{int(time.time() * 1000)}"
    try:
        r = httpx.put(url, json={"msgtype": "m.text", "body": text},
                      headers={"Authorization": f"Bearer {cfg.access_token}"}, timeout=20)
        ok = r.status_code == 200
        if not ok:
            log.warning("report send failed: %s %s", r.status_code, r.text[:200])
        return ok
    except Exception:
        log.exception("report send error")
        return False

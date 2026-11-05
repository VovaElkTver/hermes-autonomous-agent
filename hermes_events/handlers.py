"""Code-side event handlers (importable refs for the durable registry).

A registry row stores ``module:qualname`` — these functions must stay
importable at boot; renaming one parks its events loud on load, never silent.
"""

from __future__ import annotations

import logging

_log = logging.getLogger("hermes_events.handlers")


def heartbeat_probe(ts: float) -> None:
    """Smoke handler: proves the tick->queue->execute path end to end."""
    _log.info("heartbeat_probe fired at %s", ts)


def refusal_watch(ts: float) -> None:
    """Periodic (600 s): refusals of gateway inbound messages — data-driven.

    Дайджест строк 'Native admission refused' из journalctl против прошлого
    состояния (~/.hermes/events/refusal_watch.state): новый отказ = сообщение
    в тред, тишина = молчание. Cron запрещён (Вл 29.10) — только этот тикер.
    First run = baseline (не будит комнату старьём эпохи SINCE).
    """
    import json
    import subprocess
    import urllib.request
    from pathlib import Path

    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    state = home / "events" / "refusal_watch.state"
    state.parent.mkdir(parents=True, exist_ok=True)
    try:
        tok = (home / "rtx5090_token").read_text().strip()
    except OSError:
        _log.warning("refusal_watch: no token, skip")
        return
    r = subprocess.run(
        ["journalctl", "--user", "-u", "hermes-gateway", "--since",
         "2026-10-29 00:00", "--no-pager"],
        capture_output=True, text=True, timeout=30)
    lines = [ln for ln in r.stdout.splitlines()
             if "Native admission refused" in ln or "Native replay refused" in ln]
    ids = [ln.split("message=")[-1].strip() for ln in lines if "message=" in ln]
    prev = set()
    first_run = not state.exists()
    if not first_run:
        prev = set(json.loads(state.read_text()).get("ids", []))
    new = [i for i in ids if i not in prev]
    state.write_text(json.dumps({"ids": ids}))
    if first_run or not new:
        return  # baseline (не будим старьём) или тишина
    body = ("refusal-watch: новых отказов: %d. Последние: %s. "
            "Важное повтори — оно не сохранилось (текст из-за E2EE мне недоступен)."
            % (len(new), ", ".join(new[-3:])))
    room = "%21yzPjsOSdMSfUfZKtVk%3Amx.elk.mobi"
    txn = "rw%d" % int(ts * 1000)
    url = ("https://mx.elk.mobi/_matrix/client/v3/rooms/%s/send/m.room.message/%s"
           % (room, txn))
    payload = {"msgtype": "m.text", "body": body,
               "m.relates_to": {"rel_type": "m.thread",
                                "event_id": "$AjyUaH00y_pl_F2ljdk6F8Kv13O2pElAI7vw2nW3k9A"}}
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Authorization": "Bearer " + tok,
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            _log.info("refusal_watch posted: %s", resp.read()[:80])
    except Exception as exc:
        _log.warning("refusal_watch: send failed: %r", exc)

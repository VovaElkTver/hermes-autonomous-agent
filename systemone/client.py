"""L1 (TZ-memdoor-5090-v2): client for the thin decision server — donor
systemone/client.go semantics: localhost, no key, POST {origin}/v1/systemone,
fast timeout. ANY transport failure returns Decision(status='unavailable'):
a dead server is a fact, never a 'no' (Kev contract)."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from .contract import Decision, validate_choice

DEFAULT_URL = "http://127.0.0.1:8002"
DEFAULT_TIMEOUT = 2.0


def server_url() -> str:
    env = os.environ.get("HERMES_SYSTEMONE_URL")
    if env:
        return env.rstrip("/")
    try:
        from hermes_cli.config import load_config
        cfg = load_config() or {}
        return str((cfg.get("systemone") or {}).get("url") or DEFAULT_URL).rstrip("/")
    except Exception:
        return DEFAULT_URL


def _post(payload: dict, timeout: float) -> Decision:
    kind = payload["kind"]
    try:
        req = urllib.request.Request(
            server_url() + "/v1/systemone",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            blob = json.loads(resp.read().decode())
    except Exception:
        return Decision(kind, None, None, "unavailable", "transport")
    if not isinstance(blob, dict) or blob.get("status") != "ok":
        return Decision(kind, None, None, "unavailable",
                        str((blob or {}).get("reason", "server"))
                        if isinstance(blob, dict) else "malformed")
    value = blob.get("value")
    if kind == "yesno":
        value = bool(value)
    elif kind == "score":
        try:
            value = float(value)
        except (TypeError, ValueError):
            return Decision(kind, None, None, "unavailable", "malformed_value")
    return Decision(kind, value, blob.get("p"), "ok")


def decide_yesno(question: str, timeout: float = DEFAULT_TIMEOUT) -> Decision:
    return _post({"kind": "yesno", "question": str(question)}, timeout)


def decide_choice(question: str, options: list[str],
                  timeout: float = DEFAULT_TIMEOUT) -> Decision:
    err = validate_choice(question, options)
    if err:
        return Decision("choice", None, None, "unavailable", err)
    return _post({"kind": "choice", "question": str(question),
                  "options": list(options)}, timeout)


def decide_score(question: str, timeout: float = DEFAULT_TIMEOUT) -> Decision:
    return _post({"kind": "score", "question": str(question)}, timeout)

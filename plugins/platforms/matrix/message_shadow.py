"""M4 (TZ-memdoor-5090-v2): shadow message-gate for room non-mentions —
decision_gate.go semantics, shadow mode only (donor Settings: «shadow asks and
logs would_reply without waking anyone»).

When a mention-gated room drops a non-mention message, we ASK a yes/no judge
(critic endpoint today, L1 :8002 tomorrow) whether the bot would have wanted to
answer, and journal the answer. The wake decision is NOT changed — one week of
{msg_id, p_reply, would} lines gives the real false-rate for the A1 decision.

Doctrine from the donor's decision.go: an unavailable judge is a FACT, never a
negative answer — the line records p_reply=null, would=null, judge=unavailable.
Agent-authored messages are never gated (bot echo filtered upstream by the
adapter's own bot rules; the sender check below is the belt).

OFF by default (``message_shadow.enabled`` / ``HERMES_MESSAGE_SHADOW``):
observe_dropped is a single boolean check, no judge call, no file.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from collections import deque

from typing import Any

_MAX_JUDGE_MS = 2000
_CTX_WINDOW = 8

_history: dict[str, deque] = {}
_history_lock = threading.Lock()


def message_shadow_enabled() -> bool:
    env = os.environ.get("HERMES_MESSAGE_SHADOW")
    if env is not None:
        return env.strip().lower() in ("1", "true", "yes", "on")
    try:
        from hermes_cli.config import load_config
        cfg = load_config() or {}
        return bool((cfg.get("message_shadow") or {}).get("enabled"))
    except Exception:
        return False


def judge_url() -> str:
    try:
        from hermes_cli.config import load_config
        cfg = load_config() or {}
        return str((cfg.get("message_shadow") or {}).get("judge_url")
                   or "http://192.168.9.100:8012/v1/chat/completions")
    except Exception:
        return "http://192.168.9.100:8012/v1/chat/completions"


def note_message(room_id: str, sender: str, body: str) -> None:
    """Keep the last messages the adapter SAW (mentioned or not) as judge
    context — the shadow needs the window the wake path threw away."""
    if not message_shadow_enabled():
        return
    with _history_lock:
        dq = _history.setdefault(room_id, deque(maxlen=_CTX_WINDOW))
        dq.append({"from": sender, "text": str(body or "")[:400]})


def _context(room_id: str, body: str) -> list[dict]:
    with _history_lock:
        dq = _history.get(room_id)
        ctx = list(dq) if dq else []
    return (ctx + [{"from": "?", "text": str(body or "")[:400]}])[-_CTX_WINDOW:]


def ask_judge(ctx: list[dict]) -> tuple[float | None, str]:
    """Yes/no over the last messages. Returns (p_reply, status):
    p is 1.0/0.0 for yes/no; None when the judge is unreachable/refused —
    never folded into a 0."""
    convo = "\n".join(f"{m['from']}: {m['text']}" for m in ctx)
    prompt = (
        "You decide whether a chatbot should reply to the LAST message of this "
        "conversation (it did not @mention the bot). Reply with exactly one "
        "word: yes or no.\n" + convo + "\nANSWER:")
    try:
        req = urllib.request.Request(
            judge_url(),
            data=json.dumps({
                "model": "critic", "temperature": 0, "max_tokens": 2,
                "messages": [{"role": "user", "content": prompt}],
            }).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=_MAX_JUDGE_MS / 1000) as resp:
            blob = json.loads(resp.read().decode())
        text = (blob["choices"][0]["message"]["content"] or "").strip().lower()
    except Exception:
        return None, "unavailable"
    if text.startswith("yes"):
        return 1.0, "ok"
    if text.startswith("no"):
        return 0.0, "ok"
    return None, "unparsable"


def _log_line(entry: dict) -> None:
    from hermes_constants import get_hermes_home
    d = get_hermes_home() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "message_shadow.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def observe_dropped(adapter: Any, room_id: str, event_id: str,
                    sender: str, body: str) -> None:
    """Fire-and-forget shadow probe for a non-mention drop. NEVER blocks the
    sync loop and NEVER raises into the caller."""
    if not message_shadow_enabled():
        return
    ctx = _context(room_id, body)

    def run() -> None:
        try:
            p, status = ask_judge(ctx)
            _log_line({"ts": time.time(), "room": room_id, "msg_id": event_id,
                       "sender": sender, "p_reply": p,
                       "would": (None if p is None else p >= 0.8),
                       "judge": status})
        except Exception:
            pass

    threading.Thread(target=run, daemon=True,
                     name=f"msg-shadow-{event_id[:8]}").start()

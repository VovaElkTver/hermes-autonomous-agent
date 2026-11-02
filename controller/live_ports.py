"""controller.live_ports — stage003/donor tail: ports wired into the LIVE cycle.

The port classes (:class:`controller.effects.Effects`, :class:`controller.outbox.Outbox`)
were library-ready and contract-tested; this module composes them for runtime use
and gives the pump a single drain point plus an idempotent producer for task-failure
notices.

Deliverer contract (what ``Outbox`` passes/receives):
    deliver(receipt_dict) -> "sent" | raises
"gone" is produced by the caller-specific mapping; a plain ``False``/exception from
the transport is NOT "gone" — it is a retryable infrastructure failure and must
grow the attempt counter (donor semantics) instead of dropping the message.

Everything here is best-effort at the edges (a broken chat target must never
break the pump) while staying durable at the core (rows survive restarts).
"""
from __future__ import annotations

import logging

from .effects import Effects, Later
from .outbox import Outbox

logger = logging.getLogger(__name__)

#: Outbox delivery cadence: like the donor, 5 s first retries up to a 300 s ceiling.
OUTBOX_RETRY = (5.0, 300.0)

#: Upper bound of dispatch passes per pump tick (bounded tick: never wedge the
#: pump on a message whose deliverer keeps enqueuing work).
MAX_DRAIN_PASSES = 32


def alert_target_from_cfg() -> str:
    """The configured chat target for controller notices ('' = none configured)."""
    try:
        from hermes_cli.config import load_config_readonly
        ctl = (load_config_readonly() or {}).get("controller", {}) or {}
        return str(ctl.get("alert_target") or "")
    except (ImportError, OSError, ValueError):
        return ""


def default_deliver(target: str, deliver_fn=None):
    """Build the Outbox deliverer for a target line. ``deliver_fn`` overrides
    (the gateway ticker passes its captured send); otherwise the shared
    :func:`controller.alerts.deliver` path is used."""
    def _deliver(receipt: dict) -> str:
        text = receipt["text"]
        if deliver_fn is not None:
            ok = bool(deliver_fn(text))
        else:
            from . import alerts
            ok = bool(alerts.deliver(text, target))
        if ok:
            return "sent"
        # Counted as a real delivery failure (attempt +1): after
        # FAILURES_BEFORE_ERROR consecutive misses the ordering barrier lifts
        # and later messages proceed — the stuck one stays visible/pending.
        raise RuntimeError(f"chat delivery refused for target {target!r}")
    return _deliver


def build(conn, deliver, now=None) -> tuple[Effects, Outbox]:
    """Freshly composed pair bound to ``conn``; registering the outbox_reply
    handler happens in the Outbox constructor. Cheap — make them per tick."""
    effects = Effects(conn, now=now)
    outbox = Outbox(conn, deliver, effects=effects, now=now)
    return effects, outbox


def drain_pending(conn, deliver, *, max_passes: int = MAX_DRAIN_PASSES,
                  now=None) -> int:
    """Dispatch every currently-runnable effect under the CALLER's lock.

    The pump holds controller.pump_lock across its whole cycle, so two pumps
    can never drive handlers concurrently (single-flight, donor parity). Bounded
    passes keep the tick finite even with self-re-enqueueing work.
    """
    effects, _outbox = build(conn, deliver, now=now)
    total = 0
    for _ in range(max(1, max_passes)):
        done = effects.run_pending()
        total += done
        if not done:
            break
    return total


def notify_task_failed(conn, *, task_id: str, run_id: str, goal: str,
                       reason: str = "", now=None) -> bool:
    """Producer: one durable chat notice per FAILED RUN, sent via the outbox.

    Idempotent by construction (msg_id carries the run): a re-pumped tick or a
    restarted process cannot double-send. When no chat target is configured the
    notice is skipped — the journal already has the event, silence is correct.
    Returns whether a NEW notice was queued.
    """
    try:
        target = alert_target_from_cfg()
        if not target:
            return False
        goals = " ".join(str(goal or "").split())
        if len(goals) > 120:
            goals = goals[:119] + "…"
        why = " ".join(str(reason or "").split())[:200] or "worker reported failure"
        text = f"[controller] задача {task_id} провалилась (run {run_id}): {why} · {goals}"
        _effects, outbox = build(conn, default_deliver(target), now=now)
        # NOTE: no self-dispatch here — the pump tick drains right after the
        # scope cycles while still holding its lock (same-tick delivery);
        # producers stay pure writers.
        return outbox.send(f"ctl-task-{task_id}-{run_id}-failed", text,
                           conv_key=f"target:{target}", task_id=task_id)
    except Exception as exc:  # producer edge: journal already holds the truth
        logger.warning("live_ports.notify_task_failed skipped: %s", exc)
        return False
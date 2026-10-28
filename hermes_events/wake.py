"""wake — the seam from a due event to a wake turn in an existing session.

The processor never talks to the agent (26.10 contract): after an ONCE/PERIODIC
event completes, it calls ``wake_handler(event, fired_at)`` and this module
decides what, if anything, reaches an agent. The decision is purely data:
an event wakes only when its ``meta["wake"]`` names a destination.

    meta["wake"] = {"platform": "matrix", "chat_id": "!room:server",
                    "thread_id": optional, "chat_type": optional,
                    "session_id": optional raw session key,
                    "text": optional wake message (a default is used)}

``set_wake_transport(fn)`` injects the delivery mechanism — ``fn(target, text,
fired_at)`` — so the kernel stays importable without the gateway installed
(headless runs, tests). The gateway bootstrap installs
``install_gateway_transport(runner, loop)``, which routes through the SAME
wake admission the Kanban wake uses (``gateway.wake.deliver_wake``): the wake
enters the session FIFO as an internal event, never interrupting a live turn.

Failure posture: a failed delivery is logged, never raised into the executor
thread and never retried here — the event's own kind/rearm semantics own the
schedule; at-least-once is the registry's promise, not this seam's.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

logger = logging.getLogger("hermes_events.wake")

# fn(target: dict, text: str, fired_at: float) -> None
Transport = Callable[[dict, str, float], None]

_transport: Optional[Transport] = None


def set_wake_transport(fn: Optional[Transport]) -> None:
    global _transport
    _transport = fn


def get_wake_transport() -> Optional[Transport]:
    return _transport


def wake_handler(event: Any, fired_at: float) -> None:
    """Post-execution seam called by EventProcessor for ONCE/PERIODIC events."""
    meta = getattr(event, "meta", None) or {}
    wake = meta.get("wake")
    if not isinstance(wake, dict) or not wake.get("chat_id"):
        return  # silent by contract: no destination, no wake
    text = str(wake.get("text") or meta.get("wake_text")
               or f"[hermes_events] scheduled event '{event.id}' is due.")
    if _transport is None:
        logger.info("hermes_events wake for %s skipped: no transport installed", event.id)
        return
    target = {k: v for k, v in wake.items() if k != "text"}
    try:
        _transport(target, text, fired_at)
    except Exception as exc:  # the executor thread must survive delivery failures
        logger.warning("hermes_events wake for %s failed: %r", event.id, exc)


def install_gateway_transport(runner: Any, loop: Any) -> Transport:
    """Build the gateway-backed transport (delivers via live adapters + wake FIFO)."""

    def _deliver(target: dict, text: str, fired_at: float) -> None:
        import asyncio

        async def _go() -> None:
            from gateway.session import SessionSource
            from gateway.wake import adapter_supports_push, deliver_wake

            platform_name = str(target.get("platform") or "")
            member = adapter = None
            for p, a in getattr(runner, "adapters", {}).items():
                if getattr(p, "value", str(p)) == platform_name:
                    member, adapter = p, a
                    break
            if adapter is None:
                logger.warning("hermes_events wake: no live adapter for platform %r", platform_name)
                return
            source = None
            if adapter_supports_push(adapter):
                assert member is not None  # set together with adapter above
                source = SessionSource(
                    platform=member,
                    chat_id=str(target["chat_id"]),
                    chat_type=str(target.get("chat_type") or "dm"),
                    thread_id=target.get("thread_id"),
                )
            await deliver_wake(
                adapter, text=text, session_id=str(target.get("session_id") or ""),
                source=source,
            )

        try:
            fut = asyncio.run_coroutine_threadsafe(_go(), loop)
            fut.result(timeout=60)
        except Exception as exc:
            logger.warning("hermes_events wake delivery failed: %r", exc)

    return _deliver

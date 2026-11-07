"""agency plugin hooks — registers the `agency` toolset. Off by default
(``administrator.enabled``), TZ v0.2 §2. Follows the controller plugin shape.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("agency")

_ctx = None


def _enabled() -> bool:
    """Feature flag: administrator.enabled in config.yaml. read-only probe."""
    try:
        if _ctx is not None and _ctx.get_config("enabled", False):
            return True
    except Exception:
        pass
    try:
        from gateway.run_inbound import _ctl_flag_on  # same pattern controller uses
        # TODO(dev): replace with _administrator_flag_on() — dedicated top-level
        # key, owner-enforced naming (forbidden semantics in the name).
        return _ctl_flag_on()
    except Exception:
        return False


def register(ctx):
    global _ctx
    _ctx = ctx
    from . import agent_tools
    for name, schema, handler in agent_tools.tool_defs():
        ctx.register_tool(name=name, toolset="agency", schema=schema,
                          handler=handler, emoji=agent_tools.EMOJI,
                          check_fn=_enabled)
    logger.info("agency: toolset registered (flag %s)", _enabled())
    _start_loop(ctx)


_loop_worker = None


def _start_loop(ctx) -> None:
    """Loop lives between calls (TZ v0.2 §6): daemon thread owned by the plugin."""
    global _loop_worker
    if _loop_worker is not None:
        return                                   # idempotent across re-registrations
    from .loop import LoopWorker
    try:
        interval = float(_ctx.get_config("loop_interval_s", 60) if _ctx else 60)
    except Exception:
        interval = 60.0
    worker = LoopWorker(interval_s=interval)
    worker.start()
    _loop_worker = worker
    logger.info("agency: loop started (interval %.1fs)", worker.interval_s)

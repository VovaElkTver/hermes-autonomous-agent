"""agency plugin hooks — registers the `agency` toolset. Off by default
(``administrator.enabled``), TZ v0.2 §2. Follows the controller plugin shape.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("agency")

_ctx = None


def _agency_settings_enabled() -> bool:
    """Plugin setting plugins.entries.agency.settings.enabled (+ dev ctx seam)."""
    try:
        if _ctx is not None and _ctx.get_config("enabled", False):
            return True
    except Exception:
        pass
    try:
        from hermes_cli.config import load_config
        return bool(load_config().get("plugins", {}).get("entries", {})
                    .get("agency", {}).get("settings", {}).get("enabled"))
    except Exception:
        return False


def _admin_mode_on() -> bool:
    """Fork flag ``autonomous_mode`` — ТОП-УРОВЕНЬ config.yaml (Вл 08.11: «это в конфиг»,
    env-прокидывание исключено; в форке включён по умолчанию). Read live, no restart needed.
    """
    try:
        from tools.fork_mode import autonomous_mode_enabled
        return autonomous_mode_enabled()
    except Exception:
        return True  # fail-open: fork default is ON


def _agency_settings_key(key: str) -> bool:
    try:
        if _ctx is not None:
            return bool(_ctx.get_config(key, False))
        from hermes_cli.config import load_config
        entry = load_config().get("plugins", {}).get("entries", {}).get("agency", {})
        return bool(entry.get("settings", {}).get(key, False))
    except Exception:
        return False


def _enabled() -> bool:
    """Toolset visibility gate: settings-enabled AND running in admin mode."""
    return _agency_settings_enabled() and _admin_mode_on()


def register(ctx):
    global _ctx
    _ctx = ctx
    from . import agent_tools
    try:
        agent_tools.load_identity(_ctx.get_config("identity", {}) or {})
    except Exception:
        logger.warning("agency: identity load failed, keeping defaults", exc_info=True)
    logger.info("agency: acting identity %s@v%s",
                agent_tools._IDENTITY["position_id"],
                agent_tools._IDENTITY["mandate_version"])
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

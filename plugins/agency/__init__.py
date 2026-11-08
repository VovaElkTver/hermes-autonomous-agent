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
    """Fork flag ``autonomous_mode`` (admin-mode special agent stub, 04.11).

    Agency tools describe the administrator role (TZ administrator-mode §5):
    they must exist ONLY where an agent runs as administrator. Read order:
    env override → top-level config key → plugin setting
    (plugins.entries.agency.settings.autonomous_mode). Dev pilot uses the
    plugin-setting variant; the top-level key stays the production switch.
    """
    try:
        import os
        if os.environ.get("HERMES_ADMIN_MODE") == "1":
            return True
        from hermes_cli.config import load_config
        if bool(load_config().get("autonomous_mode", False)):
            return True
    except Exception:
        pass
    return _agency_settings_key("autonomous_mode")


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

"""Deafness preflight for the gateway restart phase (rtx5090-gateway-deafness-2026-11-14 R2).

An update may replace the interpreter environment a live gateway runs in (pm install-key
change, ``uv sync`` drop). If a configured platform's dependency disappears in the new
environment, restarting onto it yields an ACTIVE-BUT-DEAF unit: systemd green, humans
undeliverable (the rtx5090 41-minute incident). The restart phase calls this probe BEFORE
touching any unit: risk found -> restart stays armed as a pending obligation and the running
gateway keeps serving while the operator heals the environment.

Fail-open by design: a probe bug must never wedge the update pipeline, whose whole contract
is "the code is committed, the restart is owed". Set ``HERMES_SKIP_DEPS_PREFLIGHT=1`` to
force the restart through (escape hatch used by scheduled deploys that know better).
"""
from __future__ import annotations

import os


def deafness_risk_platforms() -> list[tuple[str, str]]:
    """``(label, repair_hint)`` for every ENABLED, REGISTERED platform whose dependency
    probe fails in this interpreter (after the registry's lazy installer had its chance).

    Returns [] when the config, registry or probe itself cannot be read — fail-open.
    Unregistered platforms are excluded: their plugin import failing is the transient
    class the reconnect watcher already heals (``_adapter_may_heal``)."""
    if os.environ.get("HERMES_SKIP_DEPS_PREFLIGHT"):
        return []
    try:
        from gateway.config import load_gateway_config
        from gateway.platform_registry import platform_registry

        config = load_gateway_config()
    except Exception:
        return []
    risky: list[tuple[str, str]] = []
    for platform, platform_config in getattr(config, "platforms", {}).items():
        try:
            if not getattr(platform_config, "enabled", False):
                continue
            if not platform_registry.is_registered(platform.value):
                continue
            entry = platform_registry.get(platform.value)
            if entry is None:
                continue
            try:
                ok = bool(entry.check_fn())
            except Exception:
                ok = False
            if not ok and entry.ensure_deps_fn is not None:
                # Same lazy installer create_adapter would run post-restart; running it
                # HERE repairs the environment while the old gateway still serves.
                try:
                    ok = bool(entry.ensure_deps_fn())
                except Exception:
                    ok = False
            if not ok:
                risky.append((entry.label, entry.install_hint or "repair the install environment"))
        except Exception:
            continue
    return risky

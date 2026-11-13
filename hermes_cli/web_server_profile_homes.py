"""Shared profile-resolution helpers for the dashboard routers.

A desktop pool backend runs one process per profile, so every dashboard write
must land inside that profile's HERMES_HOME rather than the process-wide one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import HTTPException


def _default_dashboard_profile() -> str:
    """Profile to target when a dashboard request carries no explicit ``profile``.

    A desktop pool backend runs one process per profile, but these endpoints route storage through
    the profiles tree via ``_profile_home`` — a hardcoded "default" would write a non-default
    profile's data into ~/.hermes. ``custom`` (HERMES_HOME outside the profiles tree) has no
    profile-dir equivalent, so it keeps the legacy "default" fallback.
    """
    try:
        from hermes_cli.profiles import get_active_profile_name
        name = get_active_profile_name()
    except Exception:
        return "default"
    return "default" if name in ("default", "custom") else name


def _profile_home(profile: Optional[str]) -> tuple[str, Path]:
    """Resolve a profile query value to (profile_name, HERMES_HOME)."""
    from hermes_cli import profiles as profiles_mod
    raw = (profile or _default_dashboard_profile()).strip() or "default"
    try:
        canon = profiles_mod.normalize_profile_name(raw)
        profiles_mod.validate_profile_name(canon)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not profiles_mod.profile_exists(canon):
        raise HTTPException(status_code=404, detail=f"Profile '{canon}' does not exist.")
    return canon, profiles_mod.get_profile_dir(canon)

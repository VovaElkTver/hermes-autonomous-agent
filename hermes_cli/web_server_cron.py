"""Dashboard helpers that survived the cron excision.

The cron dashboard routes (``web_routers/cron.py``) went with the ``cron``
package; only the profile-resolution helpers other routers share remain here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import HTTPException


def _cron_default_profile() -> str:
    """Profile to target when a dashboard request carries no explicit ``profile``.

    A desktop pool backend runs one process per profile, but these endpoints route storage through
    the profiles tree via ``_cron_profile_home`` — a hardcoded "default" would write a non-default
    profile's data into ~/.hermes. ``custom`` (HERMES_HOME outside the profiles tree) has no
    profile-dir equivalent, so it keeps the legacy "default" fallback.
    """
    try:
        from hermes_cli.profiles import get_active_profile_name
        name = get_active_profile_name()
    except Exception:
        return "default"
    return "default" if name in ("default", "custom") else name


def _cron_profile_home(profile: Optional[str]) -> tuple[str, Path]:
    """Resolve a profile query value to (profile_name, HERMES_HOME)."""
    from hermes_cli import profiles as profiles_mod
    raw = (profile or _cron_default_profile()).strip() or "default"
    try:
        canon = profiles_mod.normalize_profile_name(raw)
        profiles_mod.validate_profile_name(canon)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not profiles_mod.profile_exists(canon):
        raise HTTPException(status_code=404, detail=f"Profile '{canon}' does not exist.")
    return canon, profiles_mod.get_profile_dir(canon)

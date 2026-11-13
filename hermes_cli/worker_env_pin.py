"""PYTHONPATH pin for Hermes-spawned worker children (kanban dispatch et al.).

Relocated from the excised ``cron/scheduler_worker_env.py`` (keep the same
behaviour; the cron external worker it was written for is gone, but kanban
workers need the identical pin, #112729).

The shared subprocess sanitizer strips Hermes-owned PYTHONPATH entries because user
children must not see our tree. A Hermes child IS Hermes, so the pin is applied
*after* the env is built, on the sanitized env. On a self-managed (shell-installer /
PM) install the sanitizer's drop of the runtime site-packages cannot stand: the child
inherits this process's interpreter and owns no third-party dependencies of its own,
so the committed generation's site-packages is restored here too or the child dies at
its first dependency import (``No module named 'ruamel'``, #122222).
"""

from __future__ import annotations

import logging
import os
import sysconfig
from pathlib import Path

logger = logging.getLogger(__name__)


def _installed_purelib() -> Path | None:
    try:
        return Path(sysconfig.get_paths()["purelib"]).resolve()
    except (KeyError, OSError):
        return None


def _committed_dependency_site_packages(project_root: Path) -> Path | None:
    """The dependency ``site-packages`` PM committed for this install, or ``None``.

    Asked of PM's own committed selection -- the record ``activate_dependencies`` resolves
    at process boot -- rather than re-derived from this process's ``sys.path``. A runner
    that owns its dependencies (wheel / pipx / developer venv / Nix: no committed
    generation) has nothing to restore, so ``None`` means "pin the tree only".
    """
    try:
        from pm.environments import committed_venv, site_packages

        environment = committed_venv(project_root)
    except Exception as exc:
        logger.warning(
            "worker child: could not read the committed dependency environment: %s", exc
        )
        return None
    if environment is None:
        return None
    selected = site_packages(environment)
    return selected if selected.is_dir() else None


def pin_hermes_tree_on_pythonpath(worker_env: dict, repo_root: Path) -> dict:
    """Prepend ``repo_root`` -- and, when this runner has one, the committed dependency
    generation's ``site-packages`` -- to the worker env's own PYTHONPATH (never
    ``os.environ``'s).

    Skipped when ``repo_root`` is the interpreter's ``purelib``: under a wheel / pipx /
    uv-tool install the tree lives in site-packages itself, which is already importable,
    and pinning it would move site-packages ahead of the stdlib on ``sys.path``.
    """
    root = str(repo_root)
    if _installed_purelib() == Path(root).resolve():
        return worker_env
    existing = [e for e in worker_env.get("PYTHONPATH", "").split(os.pathsep) if e]
    dependency = _committed_dependency_site_packages(Path(root))
    pinned = [root, *([str(dependency)] if dependency is not None else [])]
    worker_env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys([*pinned, *existing]))
    return worker_env

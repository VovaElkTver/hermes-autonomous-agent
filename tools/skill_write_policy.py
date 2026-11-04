"""``skills.write_forbidden`` — the dumb skill-library write ban.

One rule, one place: when the config key ``skills.write_forbidden`` is true, NOTHING is
written into the skills library. Instead of writing, the content that *would* have been
written is dropped as a plain file under ``~/.hermes/need2write/`` mirroring the target
path, for a human to clean up by hand (copy-paste apply, or discard). No queues, no
pending states, no CLI, no manifests — vice-simple.

- ``false`` / key absent → the normal branch; every writer behaves exactly as before.
- ``true`` → every skill-library writer (skill_manage tool, curator transitions,
  learning contours, future mechanisms) routes its would-be write through :func:`dump`
  and returns :func:`forbidden_response` to its caller.

The key is read at action time via ``load_config_readonly()`` — its mtime/size signature
re-probes the file, so flipping the key takes effect without restarting the gateway.
Memory writes are NOT covered: this is skills only.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

NEED2WRITE_DIR = "need2write"
_CONFIG_PATH = ("skills", "write_forbidden")


def _hermes_home() -> Path:
    from hermes_constants import get_hermes_home
    return get_hermes_home()


def write_forbidden() -> bool:
    """Live read of ``skills.write_forbidden``. Errors fail OPEN (the ban is opt-in):
    a config that cannot be read must never silently freeze the library."""
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly()
        node: Any = cfg
        for key in _CONFIG_PATH:
            node = node.get(key) if isinstance(node, dict) else None
        if node is None:
            return False
        from utils import is_truthy_value
        return is_truthy_value(node)
    except Exception as e:  # a broken config read must not break writers, and must not freeze the library
        logger.debug("skill_write_policy: config read failed, treating as allowed: %s", e)
        return False


def need2write_root() -> Path:
    return _hermes_home() / NEED2WRITE_DIR


def _free_path(dest: Path) -> Path:
    """Collision suffix ``__2``, ``__3``, ... inserted before the extension (checked
    right before the write — the whole dump runs under the caller's mutation lock)."""
    if not dest.exists():
        return dest
    n = 2
    while True:
        cand = (dest.with_name(f"{dest.stem}__{n}{dest.suffix}") if dest.suffix
                else dest.with_name(f"{dest.name}__{n}"))
        if not cand.exists():
            return cand
        n += 1


def dump(rel_path: Any, content: Optional[str]) -> Path:
    """Store what would have been written to the library at ``need2write/<rel_path>``.

    *content* is the FULL final text (never a diff), with no headers added, so the file
    applies by copy-paste. ``None`` writes an empty marker file (delete/archive intents —
    the name says it). Returns the destination path actually used.
    """
    dest = need2write_root() / str(rel_path).strip("/")
    dest = _free_path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    from utils import atomic_write_text
    atomic_write_text(dest, content if content is not None else "", create_mode=0o644)
    logger.info("skill write forbidden → saved to %s", dest)
    return dest


def rel_under_root(target: Any, skill_dir: Any) -> str:
    """*target* as it lives under its skills root (local or external/bundled — the
    need2write subpath just mirrors the target path). Fallback keeps ``<skill>/<file>``.
    (Callers hold a resolved non-None pair; the broad except keeps the proposal anyway.)"""
    try:
        from tools.skill_manager_guards import _containing_skills_root
        return target.relative_to(_containing_skills_root(skill_dir)).as_posix()
    except Exception:  # lexical mismatch must not lose the proposal
        return f"{skill_dir.name}/{target.name}"


def forbidden_response(dest: Path, *, name: str, action: str) -> dict[str, Any]:
    """The agent-facing answer: success-shaped (a hard error invites blind retries), with
    an explicit do-not-retry instruction and the proposal path."""
    return {
        "success": True,
        "_forbidden": True,
        "action": action,
        "skill": name,
        "need2write": str(dest),
        "message": (
            f"Запись запрещена, предложение сохранено в {dest}. "
            f"(skills.write_forbidden: true — nothing was written to the skill library; "
            f"the folder is cleaned up by hand. Do NOT retry this write, it is a policy "
            f"ban, not an error.)"
        ),
    }

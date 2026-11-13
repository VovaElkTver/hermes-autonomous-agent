"""Fork-mode flags and the knowledge-write freeze (Вл, 08-09.11).

- ``autonomous_mode`` — ТОП-УРОВЕНЬ config.yaml (не env, не plugin setting);
  в форке включён по умолчанию. Agent-first базовый режим.
- ``administrator_mode`` — под-режим автономного (никогда не смысл без родителя);
  включает КОДОВЫЙ запрет записи/изменения MEMORY.md и SKILL.md: предполагаемая
  правка дописывается в ``<hermes home>/need2write.md``, хранилище не меняется.

Оба ключа читаются live через ``load_config`` (mtime-resign), рестарт не нужен.
По слову Вл env-прокидывание флага отсутствует намеренно."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SNIP = 400


def _cfg(key: str, default: Any) -> Any:
    try:
        from hermes_cli.config import load_config
        return load_config().get(key, default)
    except Exception:
        return default


def autonomous_mode_enabled() -> bool:
    """Top-level config key only; default ON for this fork. No env override by design."""
    return bool(_cfg("autonomous_mode", True))


def administrator_mode_enabled() -> bool:
    """Sub-flag of ``autonomous_mode`` — never ON without its parent (иерархия Вл 04.11),
    OFF by default."""
    if not autonomous_mode_enabled():
        return False
    return bool(_cfg("administrator_mode", False))


def scheduled_runtime_enabled() -> bool:
    """Root-flag gate for everything scheduled (Вл 09.11: «все наши придумки под отдельным
    корневым конфиг флагом»). In agent-first mode (default ON) the fork carries NO cron:
    no toolset surface, no ticker thread, no scheduled REST/webhook surfaces. Flip
    ``autonomous_mode: false`` to get donor upstream behavior wholesale."""
    return not autonomous_mode_enabled()


MUTATING_SKILL_ACTIONS = frozenset({"create", "patch", "edit", "write_file", "remove_file", "delete"})


def need2write_path() -> Path:
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "need2write.md"


def record_need2write(kind: str, summary: str) -> str:
    """Append an intended-but-frozen knowledge edit; returns the recording path."""
    path = need2write_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"## {stamp} kind={kind}\n{summary.strip()[:_SNIP]}\n\n")
    return str(path)


def frozen_memory_response(target: str, op: str, summary: str) -> dict[str, Any]:
    path = record_need2write("memory", f"target={target} op={op}\n{summary}")
    return {"success": True, "_frozen": True, "recorded_to": path, "target": target,
            "note": "administrator_mode: memory writes are frozen; the intent was appended to need2write.md"}


def frozen_skill_result(action: str, name: str, summary: str) -> str:
    path = record_need2write("skill", f"action={action} skill={name}\n{summary}")
    return json.dumps({"success": True, "_frozen": True, "action": action, "name": name,
                       "recorded_to": path,
                       "note": "administrator_mode: skill writes are frozen; the intent was appended to need2write.md"},
                      ensure_ascii=False)

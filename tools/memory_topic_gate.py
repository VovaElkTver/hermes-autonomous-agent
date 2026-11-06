"""memory.topic_gate — реестр тем и матчер для гейта тематической памяти.

Тиски из ТЗ v0.1 (prompt-for-rtx5090-memory-topic-gate.md): совпал префикс
реестра — запись уходит в тему; не совпал — штатная ветка байт-в-байт.
Никакого LLM на горячем пути: подстроки, glob-ы и ``re:``-регэкспы.

Конвенция match-паттернов:
- ``re:<regexp>``      — регэксп (поиск, не полный матч);
- содержит ``* ? [``   — glob против всего текста (с обёрткой «*паттерн*»);
- иначе                — подстрока.

Реестр перечитывается по mtime при каждой проверке (школа need2write:
щёлкнул файл — правило действует без рестарта). Любая ошибка чтения/парсинга
реестра = fail-open («тем нет»), ошибка пишется в лог — гейт никогда не ломает
запись памяти из-за своей инфраструктуры.
"""
from __future__ import annotations

import fnmatch
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("tools.memory_tool")

DEFAULT_REGISTRY = "~/.hermes/memories/topics.yaml"

# module-level mtime cache: {registry_path: (mtime_ns, topics)} — live reload
_CACHE: dict[str, tuple[int, list[TopicRule]]] = {}


@dataclass(frozen=True)
class TopicRule:
    name: str
    match: tuple[str, ...]
    store: str          # expanded absolute path (append-журнал темы)
    pointer: str        # точный текст строки-указателя в MEMORY.md


def _expand(p: str) -> str:
    return str(Path(p).expanduser())


def load_registry(path: str | Path) -> list[TopicRule]:
    """Topics from the YAML registry, re-read when the file changed on disk."""
    try:
        from ruamel.yaml import YAML  # house YAML lib (see hermes_cli.xai_retirement)

        path = _expand(str(path))
        try:
            stat = Path(path).stat()
        except OSError:
            _CACHE.pop(path, None)
            return []
        key = stat.st_mtime_ns
        cached = _CACHE.get(path)
        if cached and cached[0] == key:
            return cached[1]
        topics: list[TopicRule] = []
        data = YAML(typ="safe").load(Path(path).read_text(encoding="utf-8")) or {}
        for i, row in enumerate(data.get("topics") or []):
            rule = TopicRule(
                name=str(row.get("name") or f"topic-{i}"),
                match=tuple(row.get("match") or ()),
                store=_expand(row["store"]),
                pointer=str(row.get("pointer") or ""),
            )
            if rule.match and rule.store:
                topics.append(rule)
            else:
                logger.warning("topic_gate: registry row %r skipped (no match/store)", rule.name)
        _CACHE[path] = (key, topics)
        return topics
    except Exception:
        logger.warning("topic_gate: registry %s unreadable — failing open", path,
                       exc_info=True)
        if "path" in locals() and isinstance(path, (str, Path)):
            # Changed-but-broken parse = «тем нет» on every check until it parses again.
            _CACHE[str(path)] = (-1, [])
        return []


def pattern_matches(pattern: str, text: str) -> bool:
    """One match convention for the whole gate; see module docstring."""
    if pattern.startswith("re:"):
        try:
            return re.search(pattern[3:], text) is not None
        except re.error:
            logger.warning("topic_gate: bad regexp %r ignored", pattern)
            return False
    if any(ch in pattern for ch in "*?["):
        return fnmatch.fnmatch(text, f"*{pattern}*")
    return pattern in text


def resolve_topic(registry_path: str | Path | None,
                  text: str) -> Optional[TopicRule]:
    """First registered topic whose any-match hits *text*, else None.
    Registry missing/malformed → None (fail-open, штатная ветка)."""
    if not registry_path or not text:
        return None
    for rule in load_registry(registry_path):
        for pat in rule.match:
            if pattern_matches(pat, text):
                return rule
    return None


def append_entry(store_path: str, content: str) -> bool:
    """Append one §-entry to the topic's append-file (idempotent per exact text).
    Returns False on I/O failure so the caller can refuse honestly."""
    path = Path(store_path)
    delimiter = "\n§\n"
    try:
        entries: list[str] = []
        if path.exists():
            entries = [e for e in (x.strip() for x in
                                   path.read_text(encoding="utf-8-sig").split(delimiter)) if e]
        if content not in entries:  # idempotent like batch add
            entries.append(content)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.tmp-gate")
            tmp.write_text(delimiter.join(entries), encoding="utf-8")
            tmp.replace(path)
        return True
    except OSError:
        logger.warning("topic_gate: cannot append to topic store %s", store_path,
                       exc_info=True)
        return False


def read_entries(store_path: str) -> list[str]:
    path = Path(store_path)
    if not path.exists():
        return []
    delimiter = "\n§\n"
    return [e for e in (x.strip() for x in
                        path.read_text(encoding="utf-8-sig").split(delimiter)) if e]


def write_entries(store_path: str, entries: list[str]) -> bool:
    path = Path(store_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp-gate")
        tmp.write_text("\n§\n".join(entries), encoding="utf-8")
        tmp.replace(path)
        return True
    except OSError:
        logger.warning("topic_gate: cannot rewrite topic store %s", store_path,
                       exc_info=True)
        return False

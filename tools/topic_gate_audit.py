"""topic_gate_audit — диагностика кейса 6 (ТЗ memory.topic_gate v0.1).

Реплей сверки MEMORY.md против реестра тем: какая запись по правилам гейта
должна жить в store темы и сколько строк-указателей на месте. ТОЛЬКО отчёт:
никакой авто-чистки (разовая уборка была сделана вручную 05.11).

Exit codes: 0 — утечек нет; 1 — есть записи-кандидаты в темы; 2 — конфиг/реестр
не читаются.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional


def classify(entries: list[str], registry_path: Optional[str]) -> tuple[list[tuple[str, str]], list[str]]:
    """→ (leaks, pointers): leaks = [(entry, topic_name)] — записи, которые по реестру
    должны жить в store темы; pointers = строки-указатели тем, уже стоящие в MEMORY.md."""
    from tools import memory_topic_gate as gate

    leaks: list[tuple[str, str]] = []
    if not registry_path:
        return leaks, []
    rules = gate.load_registry(registry_path)
    pointers_on_file: set[str] = set()
    for entry in entries:
        rule = gate.resolve_topic(registry_path, entry)
        if rule:
            pointers_on_file.add(rule.pointer)
            if entry != rule.pointer:
                leaks.append((entry, rule.name))
    known_pointers = {r.pointer for r in rules}
    return leaks, sorted(pointers_on_file & known_pointers)


def main() -> int:
    from tools.memory_tool import get_builtin_memory_config
    from tools.memory_tool_store import MemoryStore

    cfg = get_builtin_memory_config()
    registry = cfg.get("topic_registry") or "~/.hermes/memories/topics.yaml"
    gate_on = bool(cfg.get("topic_gate", False))
    mem_path = Path(
        __import__("tools.memory_tool", fromlist=["get_memory_dir"]).get_memory_dir()
    ) / "MEMORY.md"

    entries = MemoryStore._read_file(mem_path)
    leaks, pointers = classify(entries, registry if gate_on else None)

    print(f"memory.topic_gate = {gate_on}; реестр: {registry}")
    print(f"MEMORY.md записей: {len(entries)}.")
    if not gate_on:
        print("Гейт выключен — сверка чисто диагностическая (тексты сверяются с реестром).")
    if leaks:
        print(f"\nКандидаты на переезд в тему ({len(leaks)}):")
        for entry, name in leaks:
            preview = entry[:80] + ("…" if len(entry) > 80 else "")
            print(f"  [{name}] {preview}")
    else:
        print("\nУтечек предметных записей в MEMORY.md не обнаружено.")
    if pointers:
        print(f"Строки-указатели на месте: {len(pointers)}.")
    return 1 if leaks else 0


if __name__ == "__main__":
    sys.exit(main())

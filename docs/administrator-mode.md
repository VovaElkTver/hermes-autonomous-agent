# Administrator-mode сборка автономного агента

**Статус:** реализовано в ветке `elk/administrator-mode` · **Автор:** @rtx5090 по директиве Вл 09.11 «верстай автономного агента с возможностью администратор-моде»

## Иерархия флагов (топ-левел config.yaml, только конфиг — env исключён)

```yaml
autonomous_mode: true        # базовый агент-first режим форка; default ON
administrator_mode: true     # под-режим «персональный администратор»; default OFF
```

Инвариант иерархии закреплён кодом: `administrator_mode=true` при
`autonomous_mode=false` **не включается** — под-режим без родителя недействителен.

## Что включает administrator_mode (кодовый уровень)

1. **Запись памяти заморожена.** Все мутирующие входы `MemoryStore`
   (`add/replace/patch/remove/apply_batch`) перехватываются: хранилище
   MEMORY.md/USER.md не меняется, ответ агенту success-shaped с `_frozen`.
2. **Библиотека скиллов заморожена.** `skill_manage` (все мутирующие действия,
   включая батч) не доходит до диска.
3. **Потребность фиксируется, а не теряется:** предполагаемая правка дописывается
   в `<hermes home>/need2write.md`:

```
## 2026-11-09T10:20:30Z kind=memory
target=memory op=add
факт про запрет записи…
```

4. Чтение памяти/скиллов и все остальные инструменты работают без изменений.

Реализация: `tools/fork_mode.py` (флаги + need2write), декоратор-заморозка в
`tools/memory_tool_store.py`, гейт в `tools/skill_manager_tool.py::skill_manage`.

## Отличие от skills.write_forbidden

`skills.write_forbidden` (ветка elk/skills-write-forbidden) — отдельный,
узкий механизм зеркала только для скиллов в `~/.hermes/need2write/`. Он в
администратор-мод не входит и отдельно не включается; потребность Вл.
Администратор-мод — единый режим со своей семантикой (успешный ответ агенту +
need2write.md у дома инстанса).

## Наполнение роли (следующий шаг)

Тулсет `agency` (agency_* над agency-state.db, ТЗ v0.1 §5) живёт на линии
`feature/agency-toolset-v02` / `elk/admin-mode-config-freeze` и подключается к
этой базе после выноса её автором в отдельный репозиторий и ратификации.
Видимость тулсета = plugin setting enabled И administrator_mode.

## Проверка

- регресс: `tests/tools/test_fork_administrator_freeze.py` (7 кейсов: дефолты,
  игнор env, инвариант родителя, заморозка памяти с записью в need2write,
  unfrozen-письмо, success-shaped отклик скиллам, list не заморожен);
- смежные suites `test_memory_tool`, `test_skill_manager_tool` зелёные.

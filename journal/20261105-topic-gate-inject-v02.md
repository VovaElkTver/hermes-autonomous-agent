# topic_gate v0.2 — инжекция слепка реестра в системный промпт

Дата: 05.11 · Ветка: `elk/topic-gate-inject-v02` (от `more-mem-layer`) · Мандат: вердикт
Вл через @rtx5000 («инжекция принимается сразу, пороговая схема отклоняется»).
Канон ТЗ: `~/projects/hermes-agency/docs/TZ/prompt-for-rtx5090-memory-topic-gate.md`,
раздел «Вердикт по инжекции слепка реестра в промпт (v0.2, 05.11)».

## Что сделано

Единственная точка — `tools/memory_tool_store.py::MemoryStore.load_from_disk`:
после сборки слепков MEMORY.md/USER.md, при `memory.topic_gate: true` и
непустом реестре, к слепку memory-блока дописывается блок-слепок реестра
(метод `_topic_registry_snapshot`):

- ≤4 строк на тему: name / match (через «, ») / store / pointer;
- одна строка-дисклеймер: «Registry may have changed since this session started;
  the memory tool's reply is authoritative over this snapshot»;
- значения реестра фолдятся по переводам строк (`" ".join(split())`) — YAML с
  многострочными скалярами не ломает построчную форму и не может подделать
  соседний блок промпта.

Слепок живёт в `_system_prompt_snapshot`, то есть наследует существующую механику:
заморожен при загрузке, префикс-кэш не тронут, live-reload гейта на записи
остался как был (рассинхрон закрывается дисклеймером; рестартов сессий нет).

## Приёмка (3 кейса ТЗ)

1. `topic_gate: false` → к слепку ничего не дописывается; базовый блок байт-в-байт
   (`test_prompt_snapshot_gate_off_unchanged`: gate-on блок начинается ровно с
   gate-off блока, отличается только хвостом-слепком).
2. `true` + одна тема → блок ровно 5 строк (4 + дисклеймер)
   (`test_prompt_snapshot_single_topic_block`).
3. Поведение v0.1 не изменилось: полный файл `tests/tools/test_memory_topic_gate.py`
   19 зелёных (15 старых + 4 новых); смежный регресс
   (`test_memory_tool`, `test_memory_patch_action`, `test_memory_target_contract`,
   `test_system_prompt`, `test_413_compression`) — 227 зелёных.

Плюс грани: реестра нет/сломан → слепок пустой (fail-open), многострочные поля
фолдятся (`test_prompt_snapshot_absent_registry_no_block`,
`test_prompt_snapshot_multiline_fields_folded`). Живая проверка на боевом реестре
(`~/.hermes/memories/topics.yaml`, оффлайн-вызов MemoryStore): слепок hermes-agency
рендерится 4 строками + дисклеймер после записей MEMORY.md.

## Мониторинг дрейфа

По вердикту — еженедельный реплей аудита: cron `topic-gate-drift-audit`
(id `3a547be836df`, пн 09:17, no-agent скрипт `~/.hermes/scripts/
topic_gate_drift_audit.sh`, deliver=local) запускает `tools.topic_gate_audit`
по боевой MEMORY.md. Батч-отказы учитываются агентом в journal по факту (в этом
же файле ниже). Скрипт проверен ручным прогоном: утечек нет, 9 записей.

## Статус

Коммит на ветке, БЕЗ ПУША (порядок: деплой по слову Вл). Живой шлюз продолжает
работать на more-mem-layer (v0.1) до рестарта; инжекция начнёт попадать в промпт
только после перевода чекаута/рестарта.

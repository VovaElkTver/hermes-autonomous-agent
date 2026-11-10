# managed /refine — исполнение ТЗ (правки Вл 10.11)

**Ветка:** `elk/managed-refine`, коммит `4d31c1ad60` (запушен в origin, ждёт приёмки Вл).
**Статус:** дефлой на дев/прод и включение `managed_refine: true` — только по слову Вл (§8.5).

## Скоуп в редакции Вл (важно, отличается от §0 черновика ТЗ)
- Перехватывается **весь** бэкграунд-рефайн: ручной `/refine`, авто-nudge после хода,
  idle-очередь — единая точка `_spawn_background_review_now`.
- Вариант А: каждый гейт ожидания владельца живёт ≤ `REFINE_GATE_TIMEOUT_S` = 180 c
  (константа модуля; дополнительных ключей конфига не заводить — §5).
- Молчание > таймаута → ожидающие намерения записи дописываются штатным
  `record_need2write(kind, summary)` с `run_id`, прогон → `DEFERRED_*`,
  возобновление `/refine resume <id>`.

## Что сделано
- `gateway/refine_managed.py` — машина состояний
  PREPARE→ANALYZE→PROPOSE→DISCUSS→APPLY|FAILED|STOPPED|DEFERRED_*;
  персистентность `<home>/refine_runs/<run_id>/`
  (`state.json`, `digest.txt`, `prompt_used.txt`, `response_raw.txt`);
  публикация фаз через `background_review_callback`;
  `intercept_spawn` (резидент) + `intercept_cold` (локальные сессии, transient-агент билдером);
  парсер карточек (последний ```json-блок) + кодовая тематическая валидация ДО показа
  (реестр через `memory_topic_gate.load_registry`, mtime-reread как есть).
- APPLY только штатными путями: admin-mode → `frozen_memory_response` /
  `frozen_skill_result` (+ run_id в строке need2write); иначе —
  `MemoryStore._topic_add(rule, content)` (цель — исключительно тема из реестра)
  либо `skill_manage(create|patch)`.
- ANALYZE без записи: thread-tool-whitelist перекрывается read-only набором
  (`skills_list/skill_view/read_file/search_files`) через agent-атрибут
  `_managed_readonly_whitelist`; финальный текст форка снимается в sink.
- Промпт: шаблон-константа + `{material}/{focus_block}/{specialty_context}` +
  реестр тем в тексте; override владельца замещает шаблон прогона (T2).
- Конфиг (ТЗ §5): `managed_refine` (default false), `specialty_context`,
  `refine_prompt_template`. Больше ключей нет. Таймаут — константа модуля.

## Точки входа апстрима (минимальные хуки)
- `agent/background_review.py`: параметр `prompt_override`;
  whitelist-override в `_run_review_fork`; захват финального ассистентского
  сообщения в sink после снятия `st.review_messages`; helper `_last_assistant_text`.
- `run_agent.py`: guard `intercept_spawn(...)` в начале `_spawn_background_review_now`
  (fallback на атомарное поведение при любой ошибке перехвата).
- `gateway/slash_commands_goals.py`: managed-верdictы (/refine go|stop|postpone|
  approve|reject N|status|resume, свободный текст при открытом PREPARE = поправка
  промпта), привязка `agent.session_chat_id` к ключу гейтов, cold-заглушка.
- `gateway/session_refine.py`: локальные сессии уводятся в `intercept_cold`.

## Приёмка (pytest)
- `tests/gateway/test_refine_managed.py` — T1..T7 по ТЗ; **T8 в новой формулировке**
  (авто-nudge тоже гейтится; таймаут → need2write без старта форка); T9 — флаг off =
  прежнее атомарное поведение.
- Совместно с регрессом: `test_fork_administrator_freeze.py`,
  `test_command_handler_table_smoke.py` (3 passed),
  `test_refine_focus.py`, `test_background_review.py` — суммарно 35 passed (3.6s);
  ruff по затронутым файлам чисто; pre-commit lint python зелёный на коммите.

## Ограничения / что не покрыто
- Cold/ejected-agent путь сообщений во managed-режиме отдаёт честную заглушку
  «не поддержан» (требует отдельного решения по lifetime side-агента).
- Правки карточек формулировками на DISCUSS — v1 принимает approve/reject/stop/
  postpone; редактирование диффа формулировками — следующая итерация.
- e2e `test_messaging_refine_without_cached_agent.py` чувствителен к фону машины:
  на базовом HEAD зафиксированы прогоны и ~19с, и ~132с (замер до и после отката
  правок) — деградация среда-зависимая, корректность не страдает (both green).

## Активация (по слову Вл)
1. `managed_refine: true` в config.yaml инстанса (+ опционально specialty_context).
2. Рестарт гейтвея (флаг читается live, но спавн-гвард уже в коде — рестарт нужен
   для загрузки нового кода).

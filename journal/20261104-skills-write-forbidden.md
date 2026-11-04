# skills.write_forbidden — параметр запрета записи скилов → need2write

**Дата:** 04.11.2026 · **Исполнитель:** @rtx5090 · **ТЗ:** prompt-for-rtx5090-skill-proposals.md (v2, от @vovak через @rtx5000)
**Ветка:** `elk/skills-write-forbidden` от HEAD mainline-ветки `more-mem-layer` (401f7edea2, stage-a/b уже в ней; origin/main отстал — «актуальный master» форка на момент работы = more-mem-layer).

## Что сделано (один пакет: config → guard → тесты → док)

- `tools/skill_write_policy.py` (новый) — ПРАВИЛО В ОДНОМ МЕСТЕ: живое чтение
  `skills.write_forbidden` (через `load_config_readonly`, кэш по mtime-подписи → без
  рестарта гейтвея), `dump()` в `~/.hermes/need2write/` с зеркалом целевого пути,
  коллизии `__2/__3` (суффикс перед расширением), `forbidden_response()` —
  success-образный ответ с явным «не ретраить».
- `tools/skill_manager_tool.py` — гейты в 4 чок-поинтах: `_create_skill` (до mkdir —
  в дерево не падает даже пустой каталог), `_guarded_write` (edit/patch/write_file —
  полный новый текст, не diff), `_delete_skill` (маркер `<skill>__del.md`, пустой),
  `_remove_file` (маркер `<rel>__del`). Диспетчер: `_forbidden`-результаты не идут в
  `_record_success` (ни ledger, ни usage-теллиметрии за несостоявшуюся запись).
- `tools/skill_manager_batch.py` — батч-строки результатов несут `message`/`need2write`,
  агент видит предложение в batch-ответе.
- `tools/skill_usage.py` — `save_usage` (`.usage.json` живёт ПОД skills/ — для
  побайтовой приёмки запись заморожена, вызывающий честно получает «не закоммичено»),
  `_toggle_suppressed_name`, `_relocate` (archive/restore → маркеры
  `<name>__archive.md` / `<name>__restore.md`, дерево не двигается).
- `tools/skill_ledger.py` — `append_entry` (`.curator_ledger.jsonl` тоже под skills/).
- `cli-config.yaml.example` — ключ + «папку разгребают руками».
- `tests/tools/test_skill_write_forbidden.py` — 12 тестов по всем 5 проверяемым пунктам приёмки.

Обучающие контуры (learn_prompt, insights, background_review, learning_mutations)
отдельных писателей не имеют — все идут через `skill_manage`/`_edit_skill`, покрыты.

## Отклонения/решения на ратификацию

1. **`file_tools_write_guards.py` не тронут вообще** (дифф против upstream = 0, ТЗ
   разрешал «или гвард там, или место вызова skill_manage» — выбран второй вариант).
   Следствие: прямой `write_file`/`patch` обычными файловыми тулами в ~/.hermes/skills/**
   под запретом НЕ перехватывается (у них свой гейт write_approval). Реальные писатели
   библиотеки (§3 ТЗ) через файловые тулы не пишут.
2. **Archive не описан в §2 ТЗ** — для него введён маркер `<name>__archive.md` (по тому
   же принципу «имя говорит само»), restore — `<name>__restore.md`.
3. **Теллиметрийные сайдкары под skills/** (`.usage.json`, `.curator_suppressed`,
   `.curator_ledger.jsonl`) в режиме запретa не пишутся и в need2write НЕ валятся
   (это не знание для человека; иначе need2write забился бы снапшотами JSON на каждый
   skill_view). Побайтовая приёмка #2 этим удовлетворена; `.locks/` — транзитная
   инфраструктура, из побайтового сравнения исключена (тест фиксирует).
4. **Hub-установка и bundled-sync** (`skills_hub_install`, `skills_sync*`) — механика
   человек-команд, в §3 не входит, не гейтится.
5. Батч create+write_file на ещё не созданном скиле: write_file падает на not-found и
   роллит батч, но уже сброшенное предложение create из need2write не откатывается.

## Проверки (реальные прогоны, venv репо)

- `tests/tools/test_skill_write_forbidden.py` — 12 passed (включая: config читается живым
  переключением файла без рестарта; патч-ответ «Запись запрещена, предложение сохранено
  в …»; create не оставляет каталога; `__2` при двойном прогоне; побайтовое равенство
  дерева skills/** до/после прогона `apply_automatic_transitions` с архивацией).
- Регресс в режиме false: 178 passed (skill_manager/batch/usage/ledger/size/create_dir/
  improvements), 79 passed (все test_curator*), 79 passed (file write safety),
  717 passed (все tests/tools/*skill*).
- Предсуществующие сбои хоста (воспроизведены на чистом HEAD 401f7edea2 в отдельном
  worktree): `test_fal_sdk.py` (нет fal_client), `test_vault_key_storage.py` (сборка),
  `test_skill_provenance_external.py::test_router_labels_external_vs_agent` (home_io_guard
  и scratch-путь сессии). Мои правки их не касаются.
- Не проверено: живой прогон фонового куратора на проде под запретом (тест эмулирует
  `apply_automatic_transitions`; LLM-consolidate-проход использует те же чок-поинты).

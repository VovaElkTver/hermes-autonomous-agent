# Анализ апстрим-дельты: 537 коммитов (NousResearch/hermes-agent) → влияние на autonomous-mode

**Окно анализа:** merge-base `ca43606920` → `upstream/main` `0ea6ea941a` (tip 07.11.2026; на момент постановки задачи указывался `0a0dab0686` — fetch сдвинулся, в диапазоне по-прежнему ровно **537 коммитов**, все датированы 05–07.11).
**Объём:** 1007 файлов, +48989/−30866, 228 новых файлов, 39 удалено.
**Форк:** HEAD `bc21341106`, последний sync был слиянием `upstream/main` от 06.11 (`7a16a0be54`), поэтому дельта небольшая по календарю, но плотная.
**Метод:** read-only (diff/log/blame), никаких merge/rebase/push.

---

## Наши поверхности, затронутые апстримом (с file:line)

| Наша поверхность | Где живёт у нас | Апстрим в окне | Вердикт |
|---|---|---|---|
| Хук моста в инбаунд | `gateway/platforms/base.py:2480` (`_enqueue_text_event`), вызов `gateway.task_bridge.on_owner_message` на :2488–2489 | `platforms/base.py` **не менялся ни разу** | ✅ безопасно |
| tasks_kernel bridge v0.1 | `gateway/task_bridge.py` (67 строк), коммиты `757e2d04..d19ee74e3d` | файл не существует в апстриме, конфликтов нет | ✅ безопасно |
| Инбаунд-пайплайн | `gateway/run_inbound.py` (форк: +79/−4), `gateway/run_turn.py` (форк: +29) | оба файла **не менялись** (`git diff ca43606..upstream/main` пуст) | ✅ дублей/поломок не будет |
| tk_task / tk_hold | `tasks_kernel/__init__.py:45–62` — собственный DDL `CREATE TABLE IF NOT EXISTS`, подключение голым `sqlite3` к state.db (`:65–77`, busy_timeout=5000) через `HERMES_STATE_DB`/`HERMES_HOME` | апстрим трогает только свои объекты (`messages`, триггеры, FTS) — см. ниже | ✅ таблицам ничего не грозит |
| topic-gate памяти | `tools/memory_tool_store.py:190–246` (конструктор+гейт), :338–352 (v0.2 слепок реестра в промпт), :709–717 (batch-правило); проводка: `tools/memory_tool.py:58`, `agent/agent_init.py:1244`, дефолт `hermes_cli/config_defaults.py:1315–1317` | `tools/memory_tool_store.py`: **0 апстрим-коммитов**; `system_prompt.py`: +7/−1 только внутри `_plugin_session_info` | ✅ гейт не пересекается |

Транзитные изменения инбаунда у апстрима в этом окне — косметика и вынос Юаньбао в плагин:
`relay/egress.py:160` (уточнение ModuleNotFoundError), `platforms/webhook.py:58` (убран `"yuanbao"` из builtin-платформ), `platforms/access_policy_mixin.py:1` и `platforms/helpers.py:411` (комментарии), `run_turn_runner.py:493` (+2 строки, флаг `_daybreak_surface`). Ничто из этого не задевает очередь задач или резолв toolset'ов моста.

---

## Конфликтные зоны для следующего sync

1. **`hermes_state_worker_compression.py:197` вызывает приватник, который апстрим удалил.**
   Апстрим-рефакторинг display-reads убрал `SessionMessagesMixin._with_removed_identities` и переписал `hermes_state_messages.py` (`_dedupe_display_generations(... first_ids=...)`, новый миксин `SessionStoredReadsMixin`, keyset-paging в `_display_rows_from_conn`, `_display_set` вместо ручного merge removed-rows). У нас форковый код (overlay транскрипта, `c9e10b632e`) дёргает старый метод напрямую → при sync **сломается атрибут**, нужен либо shim-алиас, либо перенос на `_display_set`.

2. **`hermes_state_common.py` — SCHEMA_SQL и триггеры.** Форк имеет 16 коммитов (ctl_causal_link, followups, session_branches и пр.), апстрим правит тот же блок схемы: дропает CJK-триггеры FTS (`refactor(state)! 0a0dab0686`, удаление `load_fts5_cjk_extension` из `hermes_state.py`) и добавляет новый `messages_display_revision_update`-триггер (`93dfe63df3`). Текстовый конфликт вокруг `CREATE TRIGGER`/SCHEMA_SQL почти гарантирован; семантика для tk_* безразлична — их объекты апстрим не трогает.

3. **`agent/agent_init.py`** — апстрим перенёс `_memory_provider_init_kwargs` (~44 строки) в модуль `agent.agent_init_memory_provider`; наша проводка topic-gate стоит рядом (:1244, около `_init_memory`). Конфликт лёгкий, но надо проследить, чтобы импорт перенесённой функции не схлопнулся с нашим локальным блоком импортов.

4. **`toolsets.py`** — апстрим удалил Yuanbao-toolset (−9 строк, включая список bundles); наш `task_queue` добавлен в смежных списках (`toolsets.py:22,130`). Конфликт тривиальный, но в общем списке бандлов (`hermes-yuanbao` строка :243 у нас ещё есть).

5. **`agent/turn_context.py`** (10 апстрим-коммитов, +71/−2: контракт `previous_message`, redaction kwargs on_turn_start, отказоустойчивый history для refused-turn) при 5 наших коммитах — следить при мерже, hunks ложатся в ту же область конструирования turn context.

6. `gateway/session.py` — апстрим снял platform-note Юаньбао (−5), у нас рядом форковые вставки (birth stamp :~7191bbe). Мелочь, но файл в overlap-списке.

Не-conflict-заметка: `memory_tool_store.py` у апстрима нулевой — наш topic-gate ветке FTS/display-reads не родственник и хук остаётся single-point.

---

## Полезные для нас новинки

- **`hermes_agent.compat.moves` + import-hook finder** (`296308546a`, файлы `hermes_agent/compat/{moves,finder}.py`) — реестр «старое имя модуля → новое», обслуживается системным finder'ом. Прямо в тему нашей боли: можно регистрировать наши переименования и перестать латать импорты после каждого sync.
- **compression-stable `conversation_id` для plugin tools/hooks** (`660eb55fc8`, `agent/prompt_cache_scope.conversation_id` в `system_prompt.py`) — стабильный ключ сквозь компакцию: кандидат на замену session_id в адресации задач tk_* и в wait_event'ах.
- **Plugin JSON-RPC методы + session-scoped события** (`aa39347712`, `gateway/session_plugin_rpc.py`) и **`llm.oneshot`** с schema/redaction (`f39868fd6a`) — готовый транспорт для controller/* портов вместо своих сокетов.
- **`previous_message` провайдерам памяти на старте хода** (`7d96784e6d` + 4 фикса) и флаг «сессия продолжена» в `MemoryManager.initialize` (`6b21d0564d`) — можно упростить тему-гейту контекст без повторного чтения транскрипта.
- **Делегат адресуем по subagent_id после жизни родителя** (`2216779700`), **транскрипт с handoff-строками и пейджингом** (`4d89901ef4`), **чтение сохранённой сессии без attach** (`95973b61d2`) — наблюдаемость долгих автономных прогонов из контроллера.
- **Разрыв петли отказов**: safety-refused ход выкидывается и заменяется чистым pre-turn baseline (`52eef32905`, `5864341f2d`) + `/undo` перечисляет изменённые файлы и ведёт к `/rollback` (`8ea5add59c`, `9ea220e0e1`) — устойчивость автономного режима при циклических отказах инструментов.
- **Remote bots в gateway-owned group chats** (`5f3cb0c441`) + отчёт peer tools в run status (`5d950e7790`) — пригодится флоту бот‑к‑боту вокруг controller-портов.
- Control socket отвечает «какой gateway обслуживает профиль» (`7d608e9647`) и `hermes gateway attach` с одноразовым тикетом (`f55d794b50`) — меньше ручного склеивания для controller-канала.

---

## Рекомендация по sync

1. **Мержить сейчас, окно удобное**: конфликтов мало, а два самых рискованных (#1, #2) лучше закрывать свежими, пока diff'ы маленькие. Схема прежняя (merge, не rebase), как в `7a16a0be54`.
2. Порядок разбора конфликтов: сначала `hermes_state_common.py` (SCHEMA_SQL + триггеры, сохранить обе стороны: и CJK-удаление, и наш `messages_display_revision_update`-аналог), затем `hermes_state_messages.py`.
3. До первого запуска после мержа: заменить вызов `SessionMessagesMixin._with_removed_identities` в `hermes_state_worker_compression.py:197` (либо shim-алиас, либо порт на `_display_set`, плюс мини-тест на overlay транскрипта).
4. После мержа сразу внести наши divergent-модули в `hermes_agent/compat/moves.py` (появится вместе с мержем) — это дешёвая страховка следующих sync'ов.
5. Обновить address-схему задач: рассмотреть переход tk_* ключей с `session_id` на `conversation_id` из prompt_cache_scope — но отдельным коммитом уже после успешного мержа.
6. Верификация: `pytest tests/tools/test_memory_topic_gate.py`, дымотест `tasks_kernel` (/qlist /qhold /qresume против рабочего state.db), контроль что `tk_task`/`tk_hold` и new trigger существуют одновременно (`sqlite3 state.db '.schema messages_display_revision_update'`).

*Готово: анализ read-only, репозиторий не изменён.*

# item1: scope-envPropagation worker → gateway (2026-11-02)

**Корень.** `hermes chat --oneshot -Q` воркера исполняет тёрн в процессе
шлюза (`gateway_chat.py`: «through the canonical gateway, never AIAgent»).
Переменные scope, которые воркер ставит в своём окружении, до тулов тёрна
не доезжали: `topic_memory_bridge.task_scope_from_env()` читал `os.environ`
шлюза, где `HERMES_SCOPE_*` нет → target='task' возвращал refusal из демона.

**Фикс (3 файлы, +22/-7).**
- `agent/session_client_env.py`: `HERMES_SCOPE_KIND`, `HERMES_SCOPE_ID`,
  `HERMES_PROJECT_ID`, `HERMES_CONTROLLER_RUN_ID` добавлены в `BEHAVIOUR_ENV`
  — клиент замораживает их в `session.create` (client_env).
- `gateway/run_turn_runner.py`: на время тёрна входит
  `policy.client_env_scope()` — тулы читают замороженный client_env.
- `tools/topic_memory_bridge.py`: чтение через `session_env()` с fallback на
  `os.environ` (CLI/cron/direct поведение не меняется).

**Приёмка (после restart шлюза 22:48).**
- Реальный oneshot-прогон с `HERMES_SCOPE_ID=verify-item1-1793649216`:
  `memory target='task' add` → `{"success": true, ..., "item_id":
  "memitem-6c189c644c"}` (до фикса — refusal).
- state.db: scope `task/verify-item1-…` создан, item в нём, binding к
  project-скоупу (`item1-verify`) — цепочка наследования живая.
- Отрицательный контроль без env: чистый structured refusal, не краш.
- Тесты: bridge/client_env/worker — 34 passed; 2 провала
  `test_session_client_env` воспроизведены на чистом HEAD (предсуществующие);
  `test_managed_worker_client_env` — flaky по 120s бюджету под параллельной
  нагрузкой, при повторе passed.

**Границы.** `memory.task_restrict_globals` остаётся OFF (тень); глобальные
targets для scoped-воркеров не трогали. Правка в чистом HEAD не проверялась
на CI — только локальный прогон.

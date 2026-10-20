# Managed-autonomy controller — команды и ограничения

Флаг: `controller.enabled: true` в config.yaml (или `plugins.entries.controller.enabled`,
или env `HERMES_CONTROLLER_ENABLED=1`). Off by default — поведение Hermes не меняется.

## Команды (распознаёт код, до агентного хода; RU/EN свободная формулировка)

| Команда | Пример формулировки | Эффект (гарантия кодом) |
|---|---|---|
| enable autonomy | «работай автономно», «приступай» | permission on, epoch+1; очередь идёт только в области workdir контракта |
| pause autonomy | «пауза», «замри, обсудим» | epoch+1 → новые автономные диспетчи block; идущему процессу interrupt_requested → подтверждение interrupted |
| resume autonomy | «продолжай автономно» | permission on, epoch+1 (старые approvals умирают) |
| cancel task | «отмени задачу task-xxxx» | task.state=cancelled, run остаётся interrupt_requested отдельно |
| agree task | «иди task-xxxx», «go task-xxxx», «dispatch task-xxxx» | MATERIAL согласован (agreed_at), draft→ready; pump диспатчит при включённой автономии scope. Голый «го/иди» без id — не команда |
| show status | «покажи статус задач» | список task/contract_version/permission |

Пауза не запрещает отдельный явно запрошенный interactive-шаг и не возобновляет очередь.

## Гарантии
- контракт MATERIAL без согласования не запускается; MATERIAL-ревизия гасит старое согласование
- review привязан к artifact hash; файлы изменились — review устарел, completed невозможен
- событие с тем же idempotency_key не повторяет побочный эффект
- бюджет per task_id, переживает рестарт и смену worker
- «worker сказал готово» без pass-review не completed
- текст документа/инструмента никогда не команда и не разрешение (origin+sender check)
- крэш-рестарт: автономность продолжается с epoch bump; /restart: interactive-режим
- незавершённые run после сбоя → unknown → обязательная сверка фактического состояния

## Вне MVP
Очередь задач + автономный loop (второй инкремент), параллельные исполнители/конкурентность §23,
inter-task события §24–26 кроме run_finished, корректировка worker без пересогласования §32,
денежные бюджеты, kanban-проекция (колонка ctl_task.kanban_ref подготовлена).

## Инструменты агента (tool surface)
Пассивный слой (хуки+skill) запрещает лишнее; инструменты дают здоровому агенту
работать с контроллером самому, без raw-SQL догадок. Регистрируются плагином
controller, видны только при `controller.enabled` (check_fn). Все идут через
controller.model / controller.transitions — одна authoritative точка:
- `ctl_status` — очередь (tasks+epoch) или разбор одной задачи (steps, контракт, события).
  Consistent read-транзакция, снимок point-in-time с `as_of`; head newest-50 + has_more.
- `ctl_enqueue` — ПРЕДЛОЖЕНИЕ задачи: draft + несогласованный MATERIAL-контракт,
  pump не диспатчит до человеческого «иди». Enqueue никогда не стартует сам.
- `ctl_step_done` — закрытие шага с read-back: результат = фактически хранящийся
  статус, «done» не предполагается; expected_status — предусловие против гонки сессий.
- `ctl_block` — running→blocked через audited transition (запрещённые переходы
  отклоняются), причина — в ctl_event.
Классификация инструментов (readOnly/destructive) — sidecar `TOOL_META` (заимствование
аннотаций из open-source Run:ai MCP-сервера): у барьера реальный потребитель —
объявленно read-only инструменты проходят, когда dispatch заблокирован (смотреть на
очередь можно всегда). Попутно найден мёртвый путь: `_default_scope` был не определён —
любой автономный вызов падал в fail-closed; теперь scope-фолбэк = cwd процесса.

## Преэмптивное переключение шага (PR-A)
Детали и аудит механизма: `docs/design/preemptive-session-control.md`.
- Control-socket verbs `interrupt-session` {session, reason, message, hard} и
  `steer-session` {session, message} (`gateway/run_session_verbs.py`): шедулер из чужого
  процесса снимает/перенаправляет живой тёрн без чат-платформы. `reason` — системная
  категория (по умолчанию `controller_preemption`, слаг через `tool_reason`, #112647):
  атрибуция тёрна говорит «scheduler preempted», НЕ user-stop. Клиенты
  `interrupt_gateway_session` / `steer_gateway_session` возвращают None на старом
  гейтвее без verbs → вызывающий идёт легаси-путём.
- `ctl_step`: жизненный цикл pending→in_progress→(paused|done) — CAS в
  `controller/model.py` (BEGIN IMMEDIATE + read-back; `mark_step_in_progress`,
  `pause_step`, `resume_step`, `unstick_step`). Pump объявляет шаг in_progress до
  диспатча; после воркера решение берётся из READ-BACK статуса шага, а не из
  слов воркера. paused-шаг невидим диспатчу и паркует задачу в paused вместо
  ложного verifying.
- Handoff-заметка «чем занимался, на какой тулзе встал» пишется при снятии тёрна
  (ctl_event `step_paused` у шага; sidecar-note у сессии) и подмешивается в шаг при
  возобновляющем диспатче — дешёвый resume с ре-анкерингом по текущему контракту.
- PR-B: `controller/preempt.py::request_preempt` — один вызов шедулера: gateway-verb
  при живом гейтвее, иначе durable fallback (флаг `interrupt_requested` на run);
  воркер-сабпроцесс режется `_kill_tree` на следующем полле `workers._await_worker`,
  не доигрывая тёрн; brief возобновлённого шага получает блок `## RESUME`.

## To-do слой (намерения над очередью, глава 11 agency-model)

Очередь (`ctl_task` + контракт/steps/приёмка/lease) несёт **обязательства**.
Слой to-do (`ctl_todo`) — **намерение-заметка** «не забыть, что придётся
заняться»: без контракта, без steps, без lease. Pump его **не видит и не
трогает** — `queue.eligible`/`next_step`/claim читают только `ctl_task` и
`ctl_step` (покрыто тестом `test_pump_claim_does_not_see_ctl_todo`).

Таблица `ctl_todo`: `todo_id, owner, text, origin_ref, deadline_hint,
status(open|materialized|dropped), created_at, materialized_task_id`.

Materialize to-do → задача = **одна атомарная операция**
(`controller.model.todo_materialize`): создаёт `ctl_task` в состоянии
draft + MATERIAL-контракт и бэклинт todo → `materialized_task_id` в одном
транзакционном коммите (как `create_task`/`clone_watch`). Повторный
materialize идемпотентен (возвращает тот же task_id). Дropped-и
materialized-to-do не материализуются; материалized нельзя drop — только
cancel задачи.

Приватность: `text` приватен другим специалистам (в статусных отчётах его
нет), но НЕ приватен инфраструктуре восстановления — менеджер видит
`owner/status/deadline_hint`. В статусной команде «покажи статус задач»
открытые todo отдаются без `text`/`origin_ref`.

CLI (`python -m controller.pump_cli todo …`):
- `todo add --owner O --text T [--origin-ref REF] [--deadline-hint HINT]`
- `todo list [--owner O] [--status open|materialized|dropped] [--full]`
  (default — менеджер-вью без `text`; `--full` — полный вью владельца)
- `todo materialize TODO_ID --workdir DIR [--owner O]`
- `todo drop TODO_ID` (soft-delete, строка остаётся для аудита)

Автоматического попадания to-do в очередь нет: draft-задача после
materialize ждёт человеческого «иди task-…», как любой draft.

## Отключение
Снять флаг + убрать controller из plugins.enabled. Ничто кроме controller/ не читает ctl_*;
таблицы можно удалить вручную (DROP TABLE ctl_*), данные sessions/triggers не затронуты.


## To-do layer (ctl_todo) — слой намерений над очередью

To-do — заметка «не забыть заняться» без контракта: ни steps, ни lease, ни приёмки.
В очередь не попадает сама: только явная операция `materialize` создаёт draft-задачу
с полным контрактом одним атомарным транзакционным шагом. Pump из `ctl_todo` не
читает вообще (`queue.eligible/next_step/claim` работают только с ctl_task/ctl_step) —
отдельный регресс-тест на слепость claim'а.

Приватность: `text` виден владельцу; статусные представления отдают только
owner/status/deadline_hint (менеджеру видно, что не забыть, а не текст заметки).

CLI:
```
python -m controller.pump_cli todo add --owner OWNER --text TEXT [--deadline-hint D]
python -m controller.pump_cli todo list [--owner O] [--status S] [--full]
python -m controller.pump_cli todo materialize TODO_ID --workdir DIR
```

Живой демо-прогон (20.10, чистый каталог, schema-ensure на connect):
```
$ todo add  --owner qwen --text '...' --deadline-hint 21.10
{"ok": true, "todo_id": "todo-d9649c99f7"}
$ todo list
{"ok": true, "todos": [{"todo_id": "todo-d9649c99f7", "owner": "qwen", "status": "open", ...}]}
$ todo materialize todo-d9649c99f7 --workdir /tmp/todo-demo/wd
{"ok": true, "todo_id": "todo-d9649c99f7", "task_id": "task-8f018c7b9c"}
$ todo list --status open
{"ok": true, "todos": []}        # задача осталась в очереди state=draft
```

Тесты: `tests/plugins/test_controller_todo.py` (жизненный цикл, privacy view,
слепость pump_claim) + общий контроллер-слайт.

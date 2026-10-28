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

## Слэш-команды этого форка

Полный реестр (унаследованные + форковые) — `website/docs/reference/slash-commands.md`.
Ниже — только те, чего нет в upstream Hermes. Все работают в gateway-сессиях.

| Команда | Синтаксис | Эффект |
|---|---|---|
| `/for` | `/for <путь\|glob> [инструкция] [--limit N] [--go]` | Принуждение каталога через очередь контроллера: итемизация каталога → одна draft-задача, `ctl_step` на каждый пункт, приёмка каждого шага судом. Workdir получает autonomy permission (`granted_via=user-command`) — сам факт ввода команды есть грант. В контракт вшит запрет батч-скриптов по каталогу: цикл — это очередь, не скрипт. Без `--go` задача ждёт «иди tid»; с `--go` согласие встроено в командную строку (contract agreed, draft→ready). Гейды `ready→running` (MATERIAL + epoch) проверяются насосом на старте. Лимит 200 пунктов по умолчанию, максимум 1000. |
| `/fork` | `/fork [name]` (алиас `/branch-lazy`) | Ленивая ветка разговора: ветка разделяет историю родителя, возвращается branch-id; выполнение продолжается параллельно основной сессии. |
| `/join` | `/join [branch-id] [--discard] [--append]` | Слияние ветки обратно: по умолчанию — работающая заметка-резюме (work-note) вместо сырой истории; `--discard` — отбросить ветку без слияния; `--append` — долить сырой хвост. |
| `/att` | `/att [заметка]` | Мягкая пауза live-turn на безопасной границе: история, pending-стёры и фоновые процессы выживают (в отличие от хард-килла `/stop`). Следующее сообщение решает развилку: «continue» — разморозить, `/stop` — отменить. Заметка передаётся модели как причина остановки. |
| `/initiate-setup` | `/initiate-setup` (алиас `/initiate_setup`) | Первый запуск: агент задаёт вопросы о пользователе и настраивает Hermes под него (имя, ОС, железо, язык), одним обычным ходом. В desktop-приложении первое сообщение само предлагает его запустить. |

Связь с очередью: `/for` создаёт ту же task-запись, что `ctl_enqueue`, но список шагов
строит код по каталогу, а не модель; отсюда «принуждение» — пропустить пункт или схалтурить
батчем можно только через явный blocked/needs_human переход, зафиксированный в журнале.

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

## Транспортный слой наблюдений (transport_observations, agency-model §16.9)

Слой между Matrix-адаптером и семантическим журналом: чинит phantom-inbound
(событие у агента есть, в комнате его нет — синтетическая вставка после
tool-result/восстановления сессии). Таблицы рядом с ctl_* в том же state.db
(SCHEMA_SQL), записи — только через `controller/transport.py`:

- `transport_observations` — сырое «транспорт доставил это»: connector/account/device,
  канал `sync|event_fetch|relations|local_echo`, raw_payload_ref (sha256 контента),
  sync_batch_id, external_event_id, observed_at.
- `event_observations` — каноническая идентичность события + 6 независимых
  характеристик (единого флага `verified` нет по спеке):
  identity_consistency / server_retrievability / decryption_status / redaction_status /
  timeline_placement / normalization_consistency.
- `reconciliation_checks` — по строке на проход сверки; неудачный fetch = результат
  **с кодом ошибки** (fetch_failed + M_код), а не «события не было»; после
  `FETCH_RETRY_LIMIT` неудач — терминальный `unverified_transport_observation`
  (error_code=retry_exhausted).

Статусы: `unverified_transport_observation` — не факт комнаты и не основание внешнего
действия (гейт внешних действий — этап 3, вне текущего PR); `synthetic_redelivery`
проставляется при записи, если то же (connector, account, external_event_id) уже
наблюдалось в другом sync-батче — синтетическая вставка отсекается этим слоем.

Писатель: Matrix-адаптер (`plugins/platforms/matrix/adapter.py`) — запись ДО любого
семантического гейтинга/журналирования (включая локальный эхо как `local_echo`),
плюс периодический `reconcile` в sync-цикле (интервал
`matrix.transport_reconcile_seconds`, дефолт 300с; refetch через room-event endpoint
того же homeserver — сверка интерфейсов, не независимая истина). Флаг по профилю:
`matrix.transport_observations: true` (или `MATRIX_TRANSPORT_OBSERVATIONS`),
выключен по умолчанию; observer-режим — любой сбой в слое не трогает приём.
Тесты: `tests/plugins/test_controller_transport.py`,
`tests/plugins/platforms/matrix/test_transport_observations.py`.


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
$ todo materialize todo-d9649c99f7 --workdir $TMPDIR/todo-demo/wd
{"ok": true, "todo_id": "todo-d9649c99f7", "task_id": "task-8f018c7b9c"}
$ todo list --status open
{"ok": true, "todos": []}        # задача осталась в очереди state=draft
```

Тесты: `tests/plugins/test_controller_todo.py` (жизненный цикл, privacy view,
слепость pump_claim) + общий контроллер-слайт.

## Ticker — резидентный тик-цикл автономии (controller.ticker)

Нити внутри процесса gateway: плагин controller при `register()` поднимает
daemon-треды (`ticker.start_default()`) — планировщик pump и НЕЗАВИСИМЫЙ
watchdog-тред со своим sqlite-подключением (залипший в run_cycle pump не
отдаёт управление планировщику; слепой к нему со-тредовой наблюдатель —
блоккер аудита 28.10); это НЕ systemd, НЕ cron и НЕ отдельный процесс.
Таблица интервалов — на monotonic clock: pump 5 минут (первый тик — через
interval/2 после старта, без drain-шторма во время прогрева гейтвея; audit
28.10), watchdog 30 минут (первый тик — по своему полному интервалу);
`max(next_anchor, now) + interval` — запоздавший тик не схлопывает каденцию
в busy-loop. Эпоха прав перепроверяется на каждом тике
(`ctl_autonomy_permission.execution_epoch`, событие
`ticker-permission-change` при смене).

`run_cycle` вызывается в той же нити (без cron- и LLM-сессий для механики).
Single-flight — сам gateway: flock `<HERMES_HOME>/run/controller-ticker.lock`
делает процесс-владелец синглтоном; ручные CLI-вызовы pump_cli и резервный
cron-тик держат общий pump-lock (`controller.pump_lock`, non-blocking,
stale-holder takeover): конкурент всегда skip, никогда не ждёт.

Heartbeats пишутся в `ctl_ticker_heartbeat(kind, ts, payload)`:
ticker-start (`pid`, intervals), pump, watchdog, ticker-stop.

Worker pgid-реестр — состояние нити: при старте открытые runs
(`ctl_run` starting/running/interrupt_requested с записанными pid/pgid)
адоптируются; обнаружение мёртвого процесса → run=interrupted, claim шага
освобождается, событие `worker-reaped`. Восстановление после рестарта
gateway — то же принятие из БД (`reconcile.recover` остаётся входом плагина).

Watchdog — детерминированный код на своём треде (перенос правил watchdog-cron
df527e45e5e2): протухший pump-heartbeat (>25 мин) → «pump замолчал» —
детектируется даже когда pump-поток не возвращается из run_cycle;
machine-park дольше 30 минут на основании `verify:` без вопросов о revise со
всеми шагами done и passing-review → возврат задачи в ready. Человеческие
гейты (`needs_human` не по `verify:`) детерминика не трогает. Gate-digest
пишется в `<HERMES_HOME>/run/pump-gates.json` каждым pump-тиком (внешний
контракт на время параллельного soak-периода).

Алерты — только фиксированные шаблоны (`controller/alerts.py`,
`ALERT_TEMPLATES`): строка человеку уходит только при СМЕНЕ digest
состояния в `ctl_alert_state`, доставка `hermes send -t <target> -q`
(без агент-цикла), стабильное состояние молчит; выздоровление чистит
digest — условие может выстрелить снова. Никакой LLM текст статуса не
сочиняет.

Soak (с 24.10): тикер-нить и cron-тик `b17bcdf067af` работают параллельно
24 часа (общий pump_lock исключает двойной drain); после отчёта cron-тик
паузится и удаляется, watchdog-cron `df527e45e5e2` удаляется (его правила
стали кодом `run_watchdog`).

## Write-Intent — согласование «что, куда, зачем и от чьего имени» (DRAFT v0)

Статус: DRAFT v0 для рецензии @vovak (26.10; перенесён сюда из `docs/design/`
27.10 — механизм внедрения существует только в рамках автономного Hermes и
не существует отдельно от описания проекта).
Проблема (аксиома Архитектора): гермес не согласовывает, что и почему он куда-то
пишет; ошибочный вывод попадает в «знания», и агент потом в них вязнет.
Усугублено тредами: годное для одного scope вредно для другого и для глобального.

### Механизм — не новый слой, а переквалификация существующего

`agent/background_review.py` уже реализует верную half: после тёрна daemon-тред
с отдельным LLM решает «сохранить ли». Его дефект — «writes go straight to the
stores». Правки:

1. Review-форк становится единственным каналом рождения решения «сохранить».
   Главный тёрн пишет не в сторы, а предлагает в этот канал.
2. Контракт выхода форка — структурированный intent, не запись в стор.
3. Между intent и стором — детерминированная сверка (scope/версия/владелец),
   затем человеческий гейт (существующая база: `tools/write_approval.py`).

### Поля intent

| поле | содержание |
|---|---|
| формулировка | знание / навык / не сохранять + почему этот мех |
| адресат | см. лестницу ниже |
| evict | для MEMORY/USER — названная жертва, либо «место у жертвы X» |
| rationale | ссылка на породившую директиву/событие |
| scope | из session key (платформа, тред-root, `:safe-` идентичность) |

Развод soul/user: «как поступаешь» → soul; «что про тебя правда» → user.

### Лестница адресатов и права записи

- `SOUL.md` — законы поведения. **propose-only**: автор один, человек.
  Эмпирический аргумент: единственный не засранный стор на флоте — тот, куда
  агент не пишет.
- `USER.md` — факты о человеке (предпочтения как свойства человека).
- `MEMORY.md` — только применимое к каждому тёрну (аксиомы, среда).
  Запись без evict-платы запрещена механизмом.
- `SKILLS` — процедуры, грузятся по входу в задачу.
- `SQLITE` — статусы, даты, длинный хвост, recall по релевантности.
- `session history` — всё преходящее (то есть почти всё).

Канал проектируем сразу под весь write-контур (файлы/конфиг/БД = proposers
позже), но v0 режет только память/скиллы.

### Иерархия решений

человек → формулировка и адресат (не diff!);
механизм → детерминированная сверка (scope, TTL, версия);
стор → запись.
Нынешний `/memory pending` — середина без начала: согласует текст, минуя причину.

### Интерфейс согласования (Element X, экран = одно решение)

- `/memory audit`: нумерованные карточки `#N (bytes) «фрагмент…» → вердикт/адресат/жертва`.
  Ответы-правки построчно: `#12 удалить`, `#19 → skill ocrmypdf`, `#7 не трогать`,
  затем `применить` — каждая правка уходит intent'ом, жертвы архивируются (не удаляются).
- Новая запись: одна карточка — заголовок формулировка → адресат → плата.
  Ответ: «да» целиком или правка поля; «нет, это знание» переопределяет мех
  и форк переформировывает карточку.
- Статус-карточка инструмента содержит rationale строкой: «✍️ writing X —
  [scope] во исполнение директивы Y» — шум лечится содержанием.

### Пересечения грамматик (регистрационная таблица; проверено по коду 27.10)

Ответы-вердикты — свободный текст, он проходит через уже живущие парсеры.
Каждый новый токен обязан быть сверен с этим списком ДО написания regex:

1. `hermes_cli/write_approval_commands.handle_pending_subcommand` — диспетчер
   `/memory|/skills`: `pending | approve|apply | reject|deny|drop | approval|mode`
   (+`diff` для skills); аргумент сверяется ТОЧНО по первому слову, неизвестное
   уходит в fallthrough. `audit` вводится как sub-команда в ЭТОМ же namespace —
   новых top-level команд не заводим.
2. `controller/commands.py::RX` — RU/EN-роутер до агентного хода
   (`controller.enabled`): pause срабатывает на «пауза|замри|обсудим», agree на
   «иди task-…». Вердикт-строка — свободный текст и дойдёт до него: «обсудим»
   внутри карточки уронит очередь автономии. Правило: пока аудит armed в этом
   тредe, verdict-парсер стоит ПЕРЕД `try_route`, а сматчившаяся строка не
   отдаётся контроллеру.
3. Inline yes/no уже занят: `/approve|/deny` опасных команд и prompt-гейт
   `write_approval`. «да» / «нет, это знание» — полный exact-match по строке,
   не подстрока, и только в armed-состоянии.
4. Matrix/Slack: клиент глотает ведущий `/` (PR #67/#68), человеческие команды
   через `!` → интерфейс печатает `!memory audit`, `/memory audit` остаётся для
   CLI/desktop.
5. Префикс `#N`: GitHub-номера задач, алиасы комнат (`#otk-vnimanie`),
   цитата-ответ Matrix (`> #12 …`). Regex'ы якорятся и толерируют blockquote:
   `^\s*>*\s*#(\d+)\s+(удалить|не трогать)\s*$`,
   `^\s*>*\s*#(\d+)\s*(?:→|->)\s*skill\s+([\w.-]+)\s*$`, `^\s*>*\s*применить\s*$`.
6. Armed-state определён узко: с момента emission карточки/дайджеста ботом в
   этом тредe до `применить` / таймаута 24 ч / явного закрытия. Вне armed
   ничего из перечисленного не парсится — обычный текст не перехватывается.

Реестр: любая новая grammar write-контура регистрируется в этой таблице и в
`website/docs/reference/slash-commands.md`; несогласованный токен в код не входит.

### Диагноз стенда (26.10, ручной аудит)

- rtx5090: 34 записи / 11456 B (лимит 9000): LIVE ~10, SKILL ~7, SQL ~12,
  DEAD ~5 → 64% не по адресу, 18% трупов.
- hermes-wsl: 29 / 7379 B: LIVE ~7, SKILL ~10, SQL ~9, DEAD ~3 → 57%.
- USER.md на обеих — по типу чисты. Болезнь локализована: MEMORY.md = вход без выхода.
- hermes-win: не снята (ssh-ключей нет), доснять через runner.

### Открытые развилки (решает Архитектор)

- Генератор предложений: review-форк или главный тёрн (или оба, с одним каналом).
- Момент включения evict-принуждения: с v0 или после первой чистки.

### Порядок работ

1. Рецензия SPEC (этот раздел).
2. Обкатка интерфейса на живом: чистка rtx+wsl по аудит-листу (командная
   реализация `/memory audit` до всякого механизма — это только дайджест+парсер).
3. v0: background_review → intent-генератор (память/скиллы), сверка + гейт.
4. Расширение на весь write-контур — по итогам обкатки.

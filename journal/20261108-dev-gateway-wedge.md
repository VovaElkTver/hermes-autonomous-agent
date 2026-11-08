# 2026-11-08 — Дев-гейтвей (@rtx5000): клин записи state.db + crash-loop

Симптом Вл (07.11 ~23:00): «сообщения читает, но не отвечает; даже /new не проходит». Затронут только HERMES_HOME=~/.hermes-dev (unit hermes-gateway-dev.service); прод (@rtx5090) не затронут.

## Факты (проверено)
1. **Клин детерминированный**: после каждого старта ≤90с один из writer-коннектов процесса держит WAL write-slot постоянно (fcntl WRITE @ byte 120 на state.db-shm, + reader-mark @124/128). Все остальные писатели получают мгновенный BUSY (busy_timeout не помогает).
2. **Жертва №1 в буте**: `CanonicalHostedRoomService.__init__ → prune_disbanded_rooms → transaction(immediate=True) @ sqlite_util.py:76` → `OperationalError: database is locked` → SupervisedTask hosted_room_worker умирает → gateway.run зовёт stop() → systemd рестартит. За ночь ~240 рестартов (NRestarts=239 к 05:10).
3. Атрибуция lockowners (c2a2a203de) сработала и назвала same-PID кандидата: `[asyncio_5] prune_disbanded_rooms@hosted_rooms.py:617 <- transaction@sqlite_util.py:76`. Это первый в очереди ожидания, не обязательно истинный вечный держатель.
4. Мой ночной штрих (03:48, стоп+TRUNCATE+удаление -wal/-shm) породил вторую аварию: FATAL split-brain guard («another process holds a deleted state.db-wal/-shm inode», errors.log с 04:00, 413 повторов). Self-healed после вымирания осиротевших держателей; Ч5 ночи гейтвей больше не падал по этой причине. Вывод подтверждён текстом доки в FATAL: удалять wal/shm только при НУЛЕ holders.
5. Инструментальная находка для будущего: мой ранний парсер /proc/locks сравнивал поля по неверным индексам (PID vs device) — «в locks пусто» было артефактом скрипта. Правильный формат: `nr: TYPE ACCESS MODE PID major:minor:inode start end`.

## Гипотеза о первопричине (не ратифицирована)
Некоторый бутовый writer держит открытую транзакцию/слот после инициализации. Кандидаты: SessionStore._execute_write/reopen при буте; FTS-путь; двойная инициализация hosted service (ensure_hosted_service + retry), гонка с prune. Для точной идентификации нужен инспекционный дамп `sqlite3.Connection.in_transaction` по gc (внутри процесса; py-spy заблокирован ptrace_scope, sudo недоступен).

## Состояние на утро
- Гейтвей перезапущен в 05:18 (pid 2888560, matrix connected), окно жизни до клина — минуты.
- wedgetrace.sh (15с сэмпл BEGIN IMMEDIATE + /proc/locks) пишет ~/.hermes/cache/scratch/wedgetrace.log ~60 мин от 05:14.
- Журналы: ~/.hermes-dev/logs/{gateway.log,errors.log,agent.log,dblock-trace.log}; boot-slices в scratch/boot-slice-*.log.

## Предлагаемый план фикса (мини-спека после аппрува)
P1: в lockowners-дамп добавить инспекцию всех sqlite3.Connection процесса (in_transaction, isolation_level, last op) — точный держатель.
P2: причина по P1 → код-фикс + регресс (бут с эмуляцией условия).
P3: prune/hosted-init сделать толерантными к BUSY (backoff-метод вместо смерти supervised-task).

— собрано агентом @rtx5090 (ночная диагностика 08.11)

## Разрешение (утро 08.11, мандат Вл «вылечи его. работай автономно»)

**Идентификация держателя.** Патч щупа в `hermes_cli/sqlite_util.py` (бюджет 12 дампов; klock-карта F_GETLK по байтам 120–132 всех трёх файлов БД; IN-TXN-детекция через gc.get_referrers + owner-path) зафиксировал единственный навечно открытый коннект с `changes≈19` и референсом `_transport_conn` — это долгоживущий коннект контроллерной модели внутри MatrixAdapter (`plugins/platforms/matrix/transport_observation.py`). Механика: `record_observation()` в дефолтном isolation-режиме выполнял INSERT+commit без явного BEGIN/rollback; любой сбой посреди последовательности (BUSY на commit/второй INSERT) проглатывался mixin'ом **без rollback** → имплицитная транзакция (и WRITER-слот WAL) оставалась открытой навсегда → все писатели state.db мгновенно BUSY.

**Фиксы (коммит fe3f955637, ветка elk/dev-gateway-wedge-fix-0811, локально):**
1. `controller/transport.py::record_observation` — явный `BEGIN IMMEDIATE` + гарантированный rollback при любом исключении; коннект вызывающего всегда чист.
2. `transport_observation.py` (mixin) — paths record и reconcile: перед глотанием исключения делается rollback разделяемого коннекта.
3. `tui_gateway/hosted_room_service.py` — джаниторский prune толерантен к BUSY: ретраи 0.5→8с в бюджете 120с вместо смерти supervised-task и рестарта шлюза.
4. Регресс `tests/controller/test_transport_observation_txn.py`: 5 кейсов (успех чист; execute-fail → нет txn и мусорных строк; commit-BUSY → откат и успешный ретрай; ValueError до транзакции; идемпотентность по event_id). `tests/controller` = 13 passed, смежные transport/causal/hosted = 56 passed.

**Живая верификация:** рестарт 06:08:56 (pid 2908156) с фиксами; тестовое сообщение ушло в DM @rtx5000 и **обслужено сквозным образом** (в комнате видны исходящие зашифрованные ответы бота); аптайм >16 мин с трафиком — раньше клин наступал ≤90с; NRestarts=0; «hosted_room_worker died» после 06:08 — ноль; внешний монитор (проба BEGIN IMMEDIATE каждые 5с) — все пробы OK, вечных WRITE-слотов не появлялось. Диагностический бюджет дампов себя исчерпал штатно.

Открытое: точное место первого утечного rollbacka (какая именно ошибка оборвала ночную запись) ретроспективно не восстановить — после фикса инцидент не воспроизводится; ветка ждёт ратификации и пуша Вл.

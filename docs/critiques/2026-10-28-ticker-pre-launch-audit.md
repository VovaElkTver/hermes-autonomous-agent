ВЕРДИКТ: NO-GO

---

## 1. Crash-process: где тикер валит или замораживает весь гейтвей

**[КРИТИЧНО] Watchdog физически не может увидеть «залипший» pump — он в том же треде.**
`ticker.py::_schedule` — последовательный цикл: `run_watchdog` выполняется между `run_pump` в ЕДИНСТВЕННОМ потоке. Комментарий в `run_watchdog` («This also covers a wedged in-thread drain: the watchdog tick is independent of the pump tick inside this same thread») — ложь: зависший внутри `loop.run_cycle` дренаж никогда не отпускает управление в `_schedule`, watchdog не тикает, `pump-stale` не срабатывает. Единственный наблюдатель молчания — тот же поток, который молчит. При этом внешний наблюдатель (cron) по условию задачи демонтируется. Итог: тихая смерть автономии без единого сигнала — ровно сценарий 24.10, который обещали закрыть.
Фикс: либо отдельный поток-watchdog с собственным conn, либо внешний минимальный проверятель (systemd timer, не LLM), который читает `MAX(ts)` из `ctl_ticker_heartbeat` и алертит.

**[КРИТИЧНО] Смерть тикер-треда перманентна, при этом flock остаётся удержанным процессом.**
`ticker.py::_run`: если `model.connect()` или первый `_heartbeat("ticker-start")` бросают исключение (а тест `test_ticker_survives_a_corrupt_ledger_at_startup` показывает, что битый sqlite — реальный кейс), `try/finally` ещё не начат → `conn.close()` и `_release_ownership()` не выполняются → `_ownership_fd` держится мёртвым тредом. `_acquire_ownership` при этом вернёт существующий fd, но вызывать `start_default()` больше некому: housekeeping-respawn (#111010) есть только у cron-тикера (`SupervisedTickerThread`), у controller.ticker супервизора нет. Процесс жив, автономия мертва, лок занят.
Фикс: обернуть всё тело `_run` до `_schedule` в try/except с heartbeat+лог; release в `finally` через отдельный try; добавить respawn на housekeeping или проверку `alive()` в существующий периодический хук.

**[СЕРЬЁЗНО] `_guard` не выживает собственной ошибки репортинга.**
`ticker.py::_guard → _tick_error`: если исходное исключение — sqlite (locked/disk full, что при истории WAL-hog этой ноды ожидаемо), то `_heartbeat` внутри `_tick_error` делает `execute+commit` на том же битом писателе и бросает второе исключение уже из except-блока → `_schedule` умирает → см. пункт выше. «The loop MUST survive any tick» не выполняется ровно на том классе отказов, ради которого он написан.
Фикс: `_tick_error` целиком в try/except с fallback в `logger` + отдельный read-only conn для error-heartbeat.

**[СЕРЬЁЗНО] Error-path коммитит полупроведённый цикл.**
Автор self-crit #2 подозревал «открытую транзакцию» — хуже: `_tick_error → _heartbeat → conn.commit()` коммитит ВСЁ недовыполненное, что успел записать `run_cycle` до исключения (emit'ы, смены статусов), вперемешку с heartbeat. Частичное состояние цикла фиксируется как свершившееся, без rollback.
Фикс: в `_guard` перед `_tick_error` — `conn.rollback()`; heartbeat писать на отдельном короткоживущем соединении.

**[СЕРЬЁЗНО] Takeover насмерть убивает собственный гейтвей.**
`pump_lock.py::_stamp_holder` пишет pid ТЕКУЩЕГО процесса (= gateway). `DEFAULT_STALE_S = 3600` при задокументированном марафон-тёрне 3595.8s (инцидент 27.09) — интервал подобран впритык. Если worker перестал polled-ить `last_active` дольше часа (длинный LLM-вызов без on_poll), любой контендер (остаточный cron soak, ручной CLI) по `_kill_stall_holders` сделает `kill_worker_tree(gateway_pid, gateway_pgid)` — убьёт весь гейтвей изнутри его же защитного механизма.
Фикс: (а) stale_s ≥ 2×максимального наблюдённого тёрна + алерт ДО килла; (б) запретить kill для holder'а с собственным pid — вместо этого «skip + alert».

**[СРЕДНЕ] `stop()` без `self._lock`** — гонка со `start()` (может джойнить уже пересозданный тред / наоборот). Мелкий, но на SIGTERM-пути.

**[МЕЛКО] `now_fn=time.time` при докриченном «monotonic clock»** (docstring Ticker): скачок NTP назад → отрицательные возрасты, пропущенные тики; вперёд → буст. Взять `time.monotonic` для каденции, wall-clock только для ts в БД.

---

## 2. WAL/locks

**[СЕРЬЁЗНО] Цепочка «WAL-hog → смерть тикера».**
Каждый `_heartbeat` — write-txn на общем state.db. При активном марафон-тёрне, держащем write-lock >20 c (факт 27.10 14:29), INSERT heartbeat ловит `database is locked` (busy_timeout в `model.connect` не показан — не доказан) → см. `[СЕРЬЁЗНО]` выше: второе исключение в `_tick_error` → смерть треда. То есть единственный задокументированный режим отказа этой ноды конвертируется в постоянную смерть планировщика.
Фикс: busy_timeout+retry на heartbeat; heartbeat в WAL-режиме на отдельном conn; деградация «heartbeat пропущен, тик жив».

**[СРЕДНЕ] SIGTERM посреди записи (дрейн 0 c).**
Мусор: (1) запись holder'а в `pump-tick.lock` остаётся с pid умершего процесса — переживается механизмом takeover (`_stale_reason` → dead holder → /proc-скан), это восстановление ЕСТЬ, засчитано; (2) `pump-gates.json` пишется in-place (`_write_gates_file`, `open(...,"w")`) — обрыв на SIGTERM оставляет битый JSON читателю-cron'у без всякой валидации. Фикс: tmp+rename. (3) Недокоммитнутые txns sqlite откатит сам — ок.

**[СРЕДНЕ] Первый тик после бута — немедленный полный дренаж** (`_schedule`: `next_pump = now`). На пересобранной БД с накопившейся очередью это шторм воркеров прямо во время прогрева гейтвея, плюс `_adopt_workers` на свежих pid'ах. Фикс: сдвинуть первый pump на interval/2, watchdog оставить на +30 мин (шторма watchdog'а, которого боится автор в self-crit #3, не будет — он и так через 30 мин).

**[МЕЛКО] `_kill_stall_holders` при dead-holder убивает ЛЮБЫХ fd-holders без age-check** — включая молодого контендера, который только что открыл файл и ещё не получил flock. Окно мало, но на ноде с soak-кроном реально.

**[МЕЛКО] `ctl_ticker_heartbeat` не чистится** — insert каждые 5 мин + на каждый skip/error. На «600+ МБ и пересобранной» БД через год это десятки тысяч строк без пользы. Добавить prune в watchdog.

---

## 3. Обязательства 24.10 против кода

**(а) stale-lock>Nмин → kill+restart — РЕАЛИЗОВАНО.** `pump_lock.py::DEFAULT_STALE_S`, `_stale_reason`, `_kill_stall_holders`, `_take_or_defer` (retake + journal `pump_lock_takeover`). Оговорка: fail-closed ветки возвращают `held` молча — серия таких «held» без алерта не видна.

**(б) дедуп ре-алертов + растущий интервал — ЧАСТИЧНО.** Дедуп есть: `alerts.py::maybe_alert` (digest в `ctl_alert_state`, clear→re-alert корректен). Растущего интервала/эскалации НЕТ нигде — повторяющийся `tick-error:pump` каждые 5 минут с изменяющимся текстом ошибки (`error=str(exc)[:120]` в дайджесте!) будет пинговать чать бесконечно. Кстати: ошибка в дайджесте ломает сам дедуп — «same error» с другим traceback-хвостом = новый алерт.

**(в) детерминированный шаблон, не LLM — РЕАЛИЗОВАНО.** `alerts.py::ALERT_TEMPLATES` + `maybe_alert`; в ticker-пути LLM-текста нет.

**(г) wake-on-red — ОТСУТСТВУЕТ.** `alerts.deliver` — это `hermes send` строки в чат. Ни пробуждения актора с правами, ни упоминания/эскалации, ни связи с barrier/permission. Алерт ≠ wake.

**(д) ручные форс-тики только с наблюдателем — ОТСУТСТВУЕТ в предъявленном коде.** `pump_cli.main()` исполняется без каких-либо подтверждений/наблюдателя; `agent_tools.tool_defs()` не показаны — проверить нельзя, а «нельзя опровергнуть» на аудите включения = нет.

Self-crit #6 подтверждается: (б) наполовину, (г)(д) — проза.

---

## 4. Двойной старт и ре-арм

Пути:
1. **Boot:** plugin `register()` → `start_default()` — `_default_lock` + `alive()` + flock. Корректно.
2. **Hot-reload плагина** (пересоздание модуля): глобусы `_default/_ownership_fd` сбрасываются, старый тред жив. Второй flock на новом fd в том же процессе провалится (flock конфликтует между OFD даже внутри процесса) → на Linux защищено; **на Windows (`os.name=="nt"` → `-1`, «start anyway») — два тикера гарантированы**. Нода Linux, но путь есть.
3. **Respawn housekeeping (#111010)** — для controller.ticker ОТСУТСТВУЕТ (тесты `test_housekeeping_restarts_a_dead_ticker` и `test_boot_starts_the_ticker_exactly_once` покрывают `cron.scheduler_provider`/`SupervisedTickerThread`, т.е. ДРУГУЮ нитку; регресс-тест 27.10 не защищает именно этот тикер). Это и путь «не запуститься вовсе»: умер — и никого.
4. **Safe mode:** проверяется один раз в `register()`. Вход в safe mode на работающем гейтее тикер НЕ останавливает; выход из safe mode тикер не запускает до перезагрузки плагина.
5. **Fork:** унаследованный `_ownership_fd` заставит ребёнка считать себя владельцем; двойной дренаж спасает только pump_lock. Не продемонстрировано, что gateway не форкается.
6. **`start_default` при `owned=None`** кладёт в `_default` тикер, чей тред мгновенно вышел (`_run` early-return) — следующий register создаст новый объект; churn под защитой flock, терпимо.

**Инвариант на уровне данных — сейчас ОТСУТСТВУЕТ** (flock — ядро, а не данные; на nt — фиктивный). Минимальный: строка-владелец в `ctl_ticker_heartbeat` (`kind='owner'`, payload `{pid, boot_token, ts}`), обновляемая каждым тиком. Стартер перед стартом: читать владельца; стартовать только если pid мёртв или `ts` старше 2×interval; захват — одним `UPDATE ... WHERE ts < ?` (CAS). Это закрывает reload, fork и Windows независимо от кода.

---

## ДЫРЫ, КОТОРЫЕ АВТОР НЕ НАЗВАЛ

- **Soak-крон — активный контендер с правом убивать гейтвей** (см. takeover-kill выше): параллельный 24h-прогон, который автор считает пассивным бэкапом, при stale-heartbeat выполнит tree-kill pid'а из записи holder'а — а там pid гейтвея.
- **Регресс-тест 27.10 защищает не тот тикер**: `tests/cron/test_ticker_startup_survival.py` — про cron-нитку гейтвея; `controller.ticker.start_default` тестом на exactly-once не покрыт вообще.
- **`pump_cli` не берёт pump_lock** в показанном коде (`main()` сразу `run_cycle`), хотя докстринг тикера утверждает «its pump attempts are already skipped by the pump lock». Если lock не берётся внутри `loop.run_cycle` — одновременный дренаж одного scope двумя путями возможен; если берётся — контракт держится на невидимом коде. Проверить до включения.
- **Ошибка попадает в дайджест алерта** (`error=str(exc)[:120]`) — переменная ошибка = вечные ре-алерты, дедуп не работает для tick-error.
- **`_check_epochs` на первом тике эмитит journal-line на каждый scope** (prev пуст) — шум в `ctl_event` при каждом рестарте.
- **PID-reuse ломает `_reap_workers`**: `_pid_liveness != "dead"` при переиспользованном pid оставляет битый run в реестре вечно.

---

## ИТОГ: FAIL

Top-3 blockers:
1. **Watchdog слеп к зависшему pump'у (один тред) + внешний наблюдатель демонтируется** — тихая смерть автономии без сигнала. Развести по тредам или оставить внешнего проверителя heartbeat'ов.
2. **Любое sqlite-исключение убивает тикер-тред навсегда** (`_tick_error` без своей защиты, `finally` без защиты, respawn отсутствует, flock утекает) — а этот класс отказов на ноде уже случался.
3. **Takeover pump_lock способен tree-kill сам гейтвей** (holder pid = gateway pid, stale_s=3600 ≈ наблюдённый тёрн 3595s) + error-path коммитит полувыполненный цикл. До включения: запрет self-kill, rollback в `_guard`, отодвинуть stale_s.

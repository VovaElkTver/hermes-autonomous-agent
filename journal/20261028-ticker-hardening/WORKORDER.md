# РАБОЧЕЕ ЗАДАНИЕ (контракт): hardening ticker-а автономного контроллера

Ты — исполнитель в изолированном git worktree `~/hermes-ticker`, ветка `elk/ticker-audit-fixes`
(база 42bc79aa5e). Ничего за пределами этого worktree не трогай: не git push, не git remote,
не ~/.hermes, не ~/hermes-agent. Все коммиты — только в текущую ветку worktree.

Контекст: плагин `controller` (резидентный ticker-тред внутри гейтвея Hermes) внешним критиком
признан NO-GO для включения на живой ноде (полный разбор — `docs/critiques/2026-10-28-ticker-pre-launch-audit.md`,
прочитай его ПЕРВЫМ). Основные файлы: `controller/ticker.py`, `controller/pump_lock.py`,
`controller/pump_cli.py`, `controller/alerts.py`, `controller/loop.py`, `plugins/controller/__init__.py`,
тесты в `tests/` (см. где лежат существующие тесты контроллера — search `controller` в tests/).

Выполни Пункты 1–12. Каждый пункт — отдельный осмысленный коммит (сообщение коммита — одна строка,
prefix `fix(ticker): ...` или `test(ticker): ...`). Код в стиле файла, комментарии на английском,
максимум сжатости. НИЧЕГО не «улучшай» сверх пунктов — лишние правки = отклонение приёмкой.

Пункт 1 (watchdog из треда): перенеси watchdog на ОТДЕЛЬНЫЙ поток с собственным sqlite-соединением;
он обязан детектировать «pump завис в run_cycle» по возрасту pump-heartbeat и алертить, даже когда
pump-поток не отдаёт управление. Тест: фейковый бесконечно-блокирующий pump, watchdog продолжает
тикать и пишет pump-stale heartbeat/алерт.

Пункт 2 (смерть треда от sqlite): `_run` целиком в try/except (heartbeat «ticker-fatal» + logger,
освобождение ownership в защищённом finally); `_tick_error` — никогда не бросает сам (try/except c
fallback в logger); перед error-heartbeat — `conn.rollback()` на основном conn, сам heartbeat на
отдельном короткоживущем соединении. Тест: база, бросающая `database is locked` на запись → тред
жив, следующий тик выполняется.

Пункт 3 (heartbeat против write-lock): busy_timeout + ограниченное число ретраев; неудачная запись
heartbeat = «skip, tick жив» + debug-лог, не исключение.

Пункт 4 (takeover не убивает себя): если pid в holder-записи == os.getpid() текущего процесса →
НЕ killpg, а skip+alert. `DEFAULT_STALE_S` подними до 7200c. Kill разрешён не раньше второго
подряд обнаружения stale (первое — только алерт «stale-suspect»). Тест: holder pid=self → kill не
вызывается.

Пункт 5 (pump_cli мимо лока): `pump_cli.main()` обязан проходить через pump_lock тем же путём,
что и ticker-путь; второй конкурент получает status «held» и выходит без run_cycle. Тест: ложный
holder живого процесса → CLI не дренирует.

Пункт 6 (шторм на буте): первый pump-tick — через interval/2 после старта, watchdog — по своему
интервалу. Никаких drain-циклов в момент прогрева.

Пункт 7 (data-инвариант exactly-once): строка-владелец в `ctl_ticker_heartbeat` (`kind='owner'`,
payload {pid, boot_token, ts}), refresh каждый тик; `start_default()` стартует только если владелец
мёртв (pid нет) или ts старше 2×интервала, захват — CAS `UPDATE ... WHERE ts < ?`; нет записи —
INSERT OR IGNORE потом CAS. EXACTLY-ONCE ТЕСТ ИМЕННО НА `controller.ticker.start_default` (реальный
тред, tmp-база): двойной вызов, повторный после re-import модуля, гонка двух start_default — ровно
один живой тред.

Пункт 8 (дедуп алертов): в digest-ключ `tick-error` не должна попадать переменная часть ошибки —
нормализуй (имя класса исключения + имя модуля); добавь растущий интервал переалертов (например
1,2,4,8… capped на суточный) с сбросом после clear. Тесты: два разных traceback одной ошибки — один
алерт; повторный алерт раньше backoff-окна — подавлен.

Пункт 9 (prune heartbeat): watchdog раз в цикл чистит `ctl_ticker_heartbeat` старше 7 дней
(owner-строку не трогать).

Пункт 10 (атомарные side-файлы): `pump-gates.json` — запись tmp+rename (os.replace); при битом
JSON читатель — деградация с логом, не исключение.

Пункт 11 (monotonic каденция): интервалы тикера — `time.monotonic()`; wall-clock остаётся только в
DB-полях ts. Проверь `_schedule`/`_now()` пути.

Пункт 12 (гонка start/stop): `stop()` под тем же `_default_lock`, что и старт; повторный stop
идемпотентен.

Финальные требования (приёмка проверяет по ним, machine-checkable):
A. `git status --porcelain` в worktree пуст (всё закоммичено).
B. Полный прогон: `./venv/bin/python -m pytest tests/ -q -k "controller or ticker or pump"` +
   полный `pytest tests/cron -q` — зелёные. (venv основной — если в worktree нет своего venv,
   зови `/home/qwen/hermes-agent/venv/bin/python -m pytest` из cwd worktree; если тесты падают по
   причине git-worktree артефакта (читают git-состояние) — зафиксируй это отдельной строкой отчёта,
   проверив тот же тест на базе.)
C. Финальный отчёт — файл `REPORT.md` в корне worktree (закоммичен последним): по каждому пункту
   1–12 строка вида `N: OK|PARTIAL|FAIL — <comit sha> — <evidence: имя теста или команда+результат>`,
   затем вывод B, затем список всего, что ты сделал ВНЕ пунктов.
D. Сообщения коммитов без слов «force», «fix CI».
Никаких новых зависимостей. Не меняй API плагина и сигнатуры public-функций, кроме случаев,
когда пункт этого требует.

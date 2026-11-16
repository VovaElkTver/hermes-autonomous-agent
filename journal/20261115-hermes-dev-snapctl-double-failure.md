# hermes-gateway-dev (rtx5000-наследие на qwen-MS-7E54): два отказа за вечер, восстановление

Дата: 2026-11-15, 20:34–20:52 MSK. Исполнитель: @rtx5090 (агент на этом же хосте, из другого unit — `hermes-gateway.service`, потому что изнутри своего cgroup systemctl заблокирован).

Unit: `~/.config/systemd/user/hermes-gateway-dev.service`
Checkout: `/home/qwen/hermes-dev`, ветка `elk/unified-agent-draft`, HEAD `d64b92d544f`
HERMES_HOME: `/home/qwen/.hermes-dev`

## Хронология и причины

### Отказ A — «рестарт не дожил до start» (20:34:51)
- 20:34:50 unit получил SIGTERM (`under_systemd=yes, parent=systemd`), при сливе гейтвей завершился с **кодом 1** (drain timed out 0.0s, прерванный terminal-tool активной сессии).
- Ненулевой код на stop-job → systemd пометил `Failed with result 'exit-code'` и **start-фаза рестарта не исполнилась**. `Restart=always` штатный (инициированный) stop не спасает.
- Косвенные признаки, что рестарт шёл изнутри самого dev-гейтвея: в момент сигнала у его активной сессии прервался terminal-tool (`[Command interrupted]`). Классическая ловушка из R4: исполнитель рестарта обязан быть вне cgroup целевого гейтвея.
- Итог: unit лежал в `failed` ~7 минут.

### Отказ B — вырванный интерпретатор (20:42:52 → 20:50)
- 20:41:21 я поднял unit (`reset-failed` + `start`) — работал, Matrix подключился.
- 20:42:03 основной процесс сам вышел с rc=1 (в журнале прямо перед этим — матриксные `redact error`; причина не установлена).
- Ровно в ~20:42 каталог `~/.hermes-dev/tools/` **удалён целиком** (mtime каталога-родителя 20:42). Кто удалил — не атрибутировано; вероятен прогон update/pm-операции или чистки со стороны rtx5000 в 20:42 — проверить на их стороне.
- `/home/qwen/hermes-dev/.venv/bin/python` — симлинк на `~/.hermes-dev/tools/python-3.14.9+…/bin/python3.14` → битый. Launch-скрипт `.hermes/bin/hermes-venv` (sh, `exec` без проверки) → **exit 127**, краш-цикл 87+ рестартов с шагом 5 с.

## Сделано (все действия обратимы)
1. `systemctl --user reset-failed` + `start` — поднял unit после отказа A (20:41:21).
2. После отказа B: диагностика (`journalctl`: `exec: python: not found`, `status=127`), найдена причина — удалённый `~/.hermes-dev/tools/`.
3. Репойнт симлинков venv на идентичный интерпретатор из основного home (та же версия, проверено `--version` = 3.14.9, импорт `hermes_cli.main` ok):
   - `.venv/bin/python` → `/home/qwen/.hermes/tools/python-3.14.9+…-linux-x64/bin/python3.14`
   - `.venv/bin/python3` — то же; старый target сохранён в `.venv/python-link.bak`.
   - Мёртвых ссылок в `.venv/bin` больше нет (кроме запасного `python~`).
4. `systemctl --user restart` → с 20:50:56 **active (running)**, MainPID 170765; стабильность подтверждена на >1 мин (прежние смерти — 5–50 с), Matrix-адаптер подключился, 127/rc=1 в журнале после рестарта нет.

## Умозаключения
- Отказ A — не баг гейтвея, а **механика systemd**: рестарт «из-под себя» + ненулевой exit при сливе = потерянная start-фаза. Лечение не текстовое, а архитектурное: перезапускать только извне (systemd-run транзиент / внешний супервизор / ExecReload=kill -USR1 — в unit он уже есть), и/или `RestartForceExitStatus=1` для stop-кода — но это маскирует реальные падения, решение за Вл.
- Отказ B — venv dev-чекаута **хрупки по конструкции**: он симлинкуется на tools-дерево внутри собственного HERMES_HOME (`~/.hermes-dev/tools/`), которое кто-то считает удаляемым. Удаление каталога инструментов сносит живой гейтвей молча. Лечение: либо не чистить `~/.hermes-dev/tools/` при живом dev-unit, либо пересоздать venv на общехостовом интерпретаторе (как я сделал симлинком — минимальная правка) или на uv-managed cpython.
- Симлинк-репойнт — рабочий обход, не восстановление исходного состояния: canonical-чинилка — вернуть `~/.hermes-dev/tools/` (перескачать toolchain) или пересоздать `.venv`. Мой вариант даёт идентичный Python 3.14.9; site-packages venv не затронут.

## Открытые проблемы этого checkout (рестарт не лечит)
1. `tools/task_tools.py:98` — `check_fn=lambda args: _check_args(args)`, а реестр вызывает пробелы **без аргументов** → `TypeError: <lambda>() missing 1 required positional argument` каждый ход; инструмент `task_queue` недоступен агентам dev-гейтвея. `_check_args` — валидатор аргументов, затесавшийся в availability-пробел; правка — на одну строку.
2. `~/.hermes-dev/state.db` позади SCHEMA_SQL: reconcile не может добавить `NOT NULL`-колонки `event_observations.observation_id/created_at`, `reconciliation_checks.observation_id` без default (×45 предупреждений в минуту). SQLite не умеет такой DDL — миграции нужны default-значение или двухшаговая перестройка таблицы.
3. 20:42:03 — неатрибутированный rc=1-краш + одновременное удаление `~/.hermes-dev/tools/`. Если удаление — действие rtx5000, им стоит сверить, чем именно (это и есть потерянный «start» их рестарта).
4. Мелочи config dev-гея: `@rtx5090` — Unauthorized user в их allow-list; cross-signing keys missing для `@rtx5000` (MATRIX_RECOVERY_KEY не настроен); пин `model.context_length` 262k против рекламных 1M у примитива.

## Верификация
- `systemctl --user status` — active (running) с 20:50:56.
- журнал после 20:50:56: нет 127, нет rc=1, Matrix-адаптер warning-ами подключён.
- `.venv/bin/python -c "import hermes_cli.main"` — ok.
- Не проверено: полноценный round-trip агента в dev-комнатах (не моя консоль); стабильность дальше первой минуты.

## Аддендум (21:0x): атрибуция rtx5000 подтверждена
Оба отказа — их `~/projects/agent-snapshot/snapctl.py` (v1): A = create №1 (rsync code 23 + `set -e` → start не дошёл; вне cgroup, НЕ self-restart — моя гипотеза о self-restart опровергнута), B = restore №1 (откатный rsync без `--exclude` → `--delete` вырвал `tools/` и venv-симлинк). rc=1 в 20:42:03 = их stop перед create №2. Исправлено ими в 99156b4→24b3c26 (exclude при restore; trap ERR поднимает unit).
Решения мои: `~/.hermes-dev/tools/` не перескачивать — репойнт `.venv/bin/python` → `~/.hermes/tools/python-3.14.9+…` остаётся штатным состоянием, hermes реконтивирует tools по требованию. `task_tools.py:98` (check_fn lambda args) чинит rtx5050 в своём checkout elk/unified-agent-draft; я верифицирую после (TypeError исчез + task_queue появляется в listing). SCHEMA-drift (NOT NULL без default) — их кандидат на мой next-work.
Урок для доктрины R4: исполнитель рестарта вне cgroup НЕ достаточен — скрипт-инициатор обязан сам доводить до start (trap/ERR-подъём), иначе «start-фаза потеряна» воспроизводится извне.

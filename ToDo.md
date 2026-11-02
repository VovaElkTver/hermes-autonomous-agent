Аксиома (Вл 29.10): архитектура hermes КРИВАЯ. Мы в долгом пути исправления hermes, а не в попытке «из говна сделать конфетку». Единственный достоверный источник данных — анализ лога сообщений matrix. Все журналы и базы самого hermes — косвенные улики.

УТОЧНЕНИЕ 01.11 (Вл): единственное достоверное свидетельство исполнения — история Matrix по client API, а в CLI — history сессии. Внешний разбор предлагал четыре разных доказательства (разрешение / принятое намерение / фактическое исполнение / увиденное человеком); по вердикту Вл все они доказываются одним и тем же источником — лентой (Matrix API / history сессии), а не внутренним состоянием: то, чего нет в ленте, не считается исполненным, что есть в ленте — не пересказывается базой. Внутренние durable-записи (write-ahead намерение, provenance join-фиксации) — обязательный механизм, но улики, не свидетельство: они обязаны сходиться с лентой, расхождение = инцидент.

Правило ведения todo-листа (Вл 29.10): мероприятия реализуются stage'ами с последовательными именами (stage000, далее по порядку); если в процессе реализации натыкаемся на косяки — дописываем в этот файл и при необходимости корректируем порядок мероприятий.

## stage001 — свой Matrix-слой (одобрено Вл 30.10)

Архитектурные аксиомы намерения:
1. **Комната — телеграф.** У комнаты нет состояний (ни «родитель», ни «дочка»); что в неё напечатали, то она и рисует. Комната - это однин из видов консли. ак cli. механищмы потоков матрикс никаким образои не являются потоками автономного агента.

## stage003 — тематическая память ядра (донор dsh-superagent @6337fc8; намерение Вл 01.11)

Намерения (дословно):
1. «этотдля нашего проекиамавтономного агента https://github.com/deepseek-ai/dsh-superagent-prototype» +
   «сейчас дам ссылку. надо тщательнотпроанализировать проект по ссылке. вплоть до анализа кода реализации.
   на возможность заимсвования компонентов и идей для ядра.» — исполнено: донор-аудит (клон
   /data/watch-repos/deepseek-ai--dsh-superagent-prototype @6337fc8, прогон тестов 1196/1197, отчёт
   ~/projects/hermes-autonomous/reports/deepseek-ai--dsh-superagent-prototype.md); порты механизмов
   (ctl_effect/outbox/mandate/event_disposition/input_receipt) в ядре коммитом `31955a1626`.
2. Два текста-предложения 01.11 (полные тексты — в ленте этого DM, аксиома свидетельства = лента;
   сжатие и сверка с кодом — в отчёте ниже): (а) маршрутизатор контекста — детекция неявных рабочих тем,
   scope-resolver корректировок, консервативная запись («отсутствие указанного scope не является
   разрешением на глобализацию»), read-gate на выдаче; (б) изоляция памяти по namespace темы —
   неизменяемый ExecutionContext (user/topic/branch/run/memory_revision), ScopedMemory с подстановкой
   scope сервером, тема берётся из задания а не из текущего чата, история/summary по темам отдельно,
   fork = snapshot+overlay, join = структурированный merge, MEMORY.md остаётся только политикой.
3. «проанализировать и дать заключение, как вставить в ядро нашего автономного агента такой слой памяти» —
   исполнено: ~/projects/hermes-autonomous/reports/topic-memory-integration.md.
4. «можно это намерение и метод реализации зафиксировать в ToDo.md как stage003?» — эта запись.

Заключение (изложение агента, обоснование — path:line в отчёте):
- Единица темы в ядре УЖЕ существует: topic_id = `ctl_task.task_id` (+`project_id` надтема),
  branch_id = `session_branches`, user_id = Hermes-профиль, run_id = `ctl_run.run_id`.
  Автодетектор неявных тем для v1 НЕ нужен — он последний шаг плана.
- Точки перехвата (проверены по коду): worker контроллера = one-shot `hermes chat --oneshot`
  (workers.py:162–180, env HERMES_CONTROLLER_TASK_ID/RUN_ID уже передан) — снапшот памяти собирается
  заново на каждый шаг, prompt-cache не задевается; memory tool получает третий target `task`
  (targets {memory,user} сейчас) с записью в SQL и provenance, глобальные targets для scoped-воркера
  read-only (механика target_enabled есть); инжекция — volatile-сегмент после static prefix
  (снапшот грузится agent_init.py:1252 / system_prompt.py:834); компрессия — существующий
  focus_topic ← title/goal задачи; межтемный доступ — контролируемый импорт reference/copy с provenance,
  семантическая похожесть тем права чтения не даёт.
- Хранение: mem_scope / mem_item / mem_binding / mem_import в каноничном SCHEMA_SQL по ctl_-конвенции
  (writers = controller package); kind ∈ fact|constraint|decision|preference; источник ∈
  user-directive|tool-result|agent-proposal|hypothesis — последние два активным правилом становятся
  только после подтверждения.
- Правило записи Вл: отсутствие указанного scope = сохранить локально (scope текущей задачи), never
  global; повторяемость — основание предложить обобщение, не доказательство универсальности.
  Иерархия применимости task → project → global(readonly); «новее затирает старое» только при
  совпадении предмета И области действия.
- Соответствие вердикту 01.11 (механизм/мысль): изоляция техническая — различает источники и области
  действия записей, reasoning не ограничивает; агент вправе искать и предлагать обобщения между темами,
  повышать scope молча не вправе; к человеку — только человеческое намерение/полномочия.

Хвост донора (исполнено 02.11, `controller/live_ports.py` + тикер): durable effects/outbox подключены
к живому циклу — pump-тик дренаит очередь ПОД своим lock'ом (single-flight, bounded passes 32);
producer «задача провалилась» = idempotent-уведомление на failed-RUN через ctl_outbox → Home-канал
(deliver_fn или alerts.deliver; отказ доставки растит attempts, барьер порядка снимается после
FAILURES_BEFORE_ERROR, по донору). Отправка в чат из тестов не стреляет (guard-фикстура).

Порядок работ (каждый шаг обратим):
1. ✅ Схема mem_* в SCHEMA_SQL + writer controller/topic_memory.py (CRUD, provenance, supersede).
   Исполнено: коммит `5fb31072af` (ctl_mem_scope/item/binding/import; чтение = SQL-фильтр scope ДО поиска).
2. Env → scope в worker_fn (HERMES_SCOPE_KIND/ID); центральной сессии pump — scope='global'.
   — исполнено тем же коммитом (`workers.scope_env`; отсутствие переменных = global на стороне чтения).
3. Memory tool: третий target `task`; ограничение глобальных writes у scoped-воркеров.
   — исполнено: `tools/topic_memory_bridge.py` + routing в `tools/memory_tool.py`
   (target 'task' появляется в схеме ТОЛЬКО при HERMES_SCOPE_*; запись через controller.topic_memory;
   источник по умолчанию agent-proposal, правила — после confirm; гвард глобальных writes
   memory.task_restrict_globals, по умолчанию OFF до shadow-приёмки).
4. Инжекция тематического блока при сборке промпта (global-политика + project/task блоки из SQL,
   фильтр scope ДО поиска) — исполнено: volatile-сегмент после memory-блоков
   (`agent/system_prompt._topic_memory_part` → bridge.render_prompt_block; без env — ноль байт).
5. focus_topic компрессии ← title/goal задачи; summary шагов линковать через ctl_artifact_revision,
   не дублировать в память.
   — исполнено (первая половина): `tools.topic_memory_bridge.task_goal_topic` приоритетнее
   эвристики `_derive_auto_focus_topic` в `agent/context_compressor_summary.py:89` только у
   scoped-процессов; prepared_compaction (интерактивные fork'и) не тронут — там нет scope-env.
6. Shadow-режим: тематические записи пишутся параллельно со старой логикой MEMORY.md; счёт трёх ошибок
   Вл — (i) локальная корректировка стала глобальной, (ii) чужое правило попало в контекст, (iii) при
   возврате к теме не найдено ранее принятое решение. Первые две дороже третьей; политика консервативна.
   — каркас исполнен: таблица ctl_mem_shadow + writer `controller/topic_memory_shadow.py`;
   авто-детекторы: (i) scoped-запись через builtin store при OFF-гварде, (iii) подтверждённые правила
   за шапкой рендера (_MAX_PROMPT_ITEMS=40); (ii) в v1 — ручная разметка (нужна семантика);
   поведение shadow НЕ меняет; интерпретация и приёмка — Вл.
7.  детектор неявных тем для интерактивного агента (наблюдателем, по образцу
   embedding-классификатора из разбора 01.11).
   — каркас исполнен (`bebbe94f5b`): controller/topic_detector.py — на каждый
   интерактивный user-оборот одна строка в ctl_topic_detect (corpus = активные
   ctl_task goals + прошлые предложения; embed через эндпоинт плагина
   sqlite-memory, lexical-фолбэк; fail-open). Хук в gateway _run_agent_inner,
   отдельный поток; heartbeat'ы и HERMES_SCOPE-процессы пропускаются.
   Гейт memory.topic_detect_enabled по умолчанию OFF — ноль строк, ноль HTTP.
   Классификация НЕ читается в поведение до приёмки Вл (шаг 6 тени).

Урок item1 (инцидент 02.11, PATH-замечание Вл): `hermes chat --oneshot -Q` воркера
исполняет ход В ПРОЦЕССЕ ШЛЮЗА (gateway-hosted turn) — env-зависимости ядра возить
только именованными переменными через client_env/BEHAVIOUR_ENV, не через окружение
процесса. PATH-канал тулчейна (TOOL_ENV, client_env_passthrough) — best-effort для
интерактивных тулов: conda/uv/venv'ы тусуют PATH как хотят, на нём ничего не строить;
тесты не должны зависеть от окружения запуска (герметизировать monkeypatch'ем
`_HERMES_BIN_DIR`, см. tests/gateway/test_session_client_env.py).


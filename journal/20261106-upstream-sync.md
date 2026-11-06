# upstream-sync-20261106 — дельта 1326 (main..upstream/main), гейты в работе

Дата: 06.11 · Ветка: `elk/upstream-sync-20261106` · Мандат: «делай» (Вл, 06.11)

## База

Вчерашняя цепочка: main = 6307081868 (донор 6ab0de91d8) + f4520799d2 (more-mem-layer).
Дельта против **main**: 1326 коммита (прошлый отчёт upstream-watch считал против
more-mem-layer — 3189, двойной счёт донора; формулировку watcher'а меняю на дельту от main).

## Ход слияния

1. `elk/upstream-sync-20261106` ← main; merge upstream/main @ ca4360692055.
   Конфликты: только 2 докованных справочника slash-commands (en + zh-Hans) —
  их сторона возродила write-approval месседжинг-подкоманды /skills, наша держала
   `/save` в CLI-only списке. Разрешено юнионом: структура+формулировки upstream,
   `/save` сохранён.
2. Порядок-итог: первая попытка была основана только на main (topic-gate туда не
   входил) — исправлено дослиянием more-mem-layer@eb04f3a4da; итоговый HEAD
   376a17d9f2 = main + upstream/main + more-mem-layer (v0.1+v0.2 садятся поверх
   свежего апстрима без отдельного бэкпорта).

## Что апстрим изменил в run_inbound.py (зона автоконфликта)

- блок admission вынесен в метод `_hm_admit_to_fifo()` + телеметрия busy-прихода
  (`stamp/settle_busy_arrival`);
- наш фикс 29.10 (`rescued_callback` вокруг `admit_message`) auto-merge перенёс
  внутрь нового метода корректно — проверено чтением hunk'а;
- остальное по файлу: бот↔бот идентичность/хопы, SessionActor переименование,
  relay attachment filenames.

## Гейты

- Gate-A (адресные): topic-gate 19, memory_tool, authority_busy plaintext/preempt —
  89 passed.
- tests/controller: 8 passed.
- Gate-B (tests/hermes_state): гоняется фоном, лог
  `~/.hermes/cache/scratch/hermes_state_gate2.log` — итог дописывается ниже.

## Gate-B итог (06.11 ~10:35, тик)

`tests/hermes_state/` целиком: **2 failed, 1789 passed, 59 skipped, 1 error** (2623 с).
Разбор двух находок:

1. `test_local_lineage_cache.py::test_read_snapshot_does_not_outlive_its_transaction`
   — падает детерминированно (в изоляции тоже). Проверка на main@f4520799d2
   (scratch-worktree, PYTHONPATH=, .venv): падает ИДЕНТИЧНО → предсуществующая
   находка с 05.11, НЕ регрессия сегодняшнего слияния. Вероятная первопричина —
   окружение: хостовый SQLite 3.50.4 форкает на `journal_mode=DELETE` (WAL-reset
   баг, warning в логе прогона), а тест 3e8e7bd3bf (апстрим 23.10) написан под
   snapshot-изоляцию WAL. Класс environment-red.
2. `test_state_db_repair_non_destructive.py::test_interrupted_snapshot_rolls_back_destination`
   — в изоляции ПРОХОДИТ. Стопок в полный прогон = StopIteration глобального
   monkeypatch `time.monotonic` (двухэлементный тик-итератор выжигает фоновый
   поток другого теста). Класс flaky-ordering, не регрессия.

Мerged-only регрессий нет → Gate-B принят с двумя классифицированными находками
(обе — очередь долгов не этого слияния; lineage-тест стоит отдельной строкой
Вл на ратификацию environment-классификации).

## Статус

Gate-A и Gate-B закрыты (этот тик додрал Gate-B, висел «ждёт агента»). Ветка
запушена в origin, ls-remote сверен. Шлюз продолжает жить на
more-mem-layer@0c285095d1 (v0.2, перезапуск 08:58 прошёл — MainPID сменился,
инцидент PM-install-key закрыт самим фактом цикла). Перевод чекаута прода и
рестарт на sync-ветку = шаг приёмки Вл.

## Gate-B вердикт (дополнение)

`tests/hermes_state` целиком на merged HEAD: **2 failed + 1 error, 1789 passed,
59 skipped** за 43m51s. Единственный красный узел —
`test_state_db_repair_non_destructive.py::test_interrupted_snapshot_rolls_back_destination`
(StopIteration в исчерпавшемся генераторе ticks у monkeypatched `time.monotonic`,
fail+teardown-error — один корень). Классификация: **order-flake** — узел зелёный
в изоляции и на базе (f4520799d2, проверено отдельным worktree), и на merged HEAD.
Регрессий от слияния нет; передеплой не блокирует.

Чекаут основного репо возвращён на more-mem-layer (правило «живой шлюз =
проверенный код»: краш-рестарт поднимет то, что загрузилось в 08:58).

## Приёмка-деплой 06.11 12:08 (наблюдение autonomy-тика)

Параллельный деплой-контур (session/pm-цикл, не этот тик) в 12:08:04–12:08:48
исполнил перевод прода: checkout more-mem-layer→main, `hermes pm sync`
(exit 0, venv_rebuild ok, receipt pm_20261106T090812Z), merge
elk/upstream-sync-261106 → main (`aef4e5acea`, ort), refresh checkout, push.
Сверено ls-remote: `origin/main == aef4e5acea` (содержит more-mem-layer
eb04f3a4da + topic-gate v0.2 0c285095d1 + upstream-дельту 1326 + Gate-B
вердикт 5e8e71120a); `origin/elk/upstream-sync-20261106 == 5e8e71120a`;
дерево чистое, не запушенного нет. Шлюз на момент тика жил ещё сборкой
08:58:55 (more-mem-layer@eb04f3a4da) — рестарт деплой-контур за собой не
оставил. Тик смонтировал самозакрывающийся guarded-рестарт:
autonomy-gw-restart-261106.timer (~12:14 MSK), скрипт
gw-restart-sync-261106.sh делает no-op, если шлюз уже перезапускался или
чекаут сдвинулся; след в journalctl (`gw-restart-sync`). Логи шлюза за
15 мин тика: 0 ERROR/Traceback (только собственные execute_code-BLOCKED
WARNING). ToDo.md 118 строк: «ждёт агента»/~~самозакрывов нет.

## Рестарт подтверждён 06.11 12:22 (autonomy-тик)

Guarded-рестарт сработал штатно: `ExecMainStartTimestamp=12:14:38`, юнит
`autonomy-gw-restart-261106` — dead/elapsed (самозакрывающийся), шлюз
active/running на merged main (чекаут 54037d7b3f). Логи с момента старта:
ни ERROR, ни Traceback (Matrix-коннект рабочий, кросс-сайнинг WARNING —
штатные). Штук-работы тика: `origin/main == HEAD == 54037d7b3f`, 0 непушнутых;
`ToDo.md` во fleet/upstream-sync идентичен main (diff пуст — догонять нечего).
Внешние cron-воркеры: hermes-mcp-listing-watchdog ok/0 (самоочистился, как
прогнозировалось); daily-trace-harvest и idle-intake-interesting — streak=1
от прогонов 23:30/00:30 (ДО pm-репра 08:58), та же подписка
NoCommittedEnvironment — не триажим, самоочистятся tonight. Деплой-контур
upstream-sync-20261106 закрыт на нашей стороне; остаётся приёмка Вл.

## Autonomy-тик 13:2x (06.11)

Дерево чисто; `main == origin/main == 8bed355724` (ls-remote), непушнутого нет
(разрыв `origin/fleet/upstream-sync..HEAD` — стылость ToDo-лейна ветки fleet,
содержимое целиком в main, догонять нечего). Шлюз active c 12:14:38, за 15 мин
0 ERROR/Traceback; 4 «Matrix: sync error» за час (13:06 эgress-блипы, сами
рассосались, sync-токен двигается) — не инцидент. «ждёт агента»/~~самозакрывов
в ToDo и свежих journal-секциях нет; все ветки == origin.Остаётся только
приёмка Вл (upstream-sync merge, causal V-O9, skills-write-forbidden).

## Autonomy-тик 13:5x (06.11)

Дерево чисто; `main == origin/main == 735cb1595d` (fetch+ls-remote), непушнутого
нет. Все тени == origin: elk/skills-write-forbidden 464e76d36f, elk/topic-gate
ce2f83975d, elk/topic-gate-inject-v02 0c285095d1, elk/upstream-sync-20261106
5e8e71120a, more-mem-layer eb04f3a4da; elk/ticker-audit-fixes — предок HEAD.
Шлюз active c 12:14:38, за 15 мин «-- No entries --» (ни ERROR, ни Traceback).
ToDo.md во fleet/upstream-sync идентичен main (diff пуст). «ждёт агента» и
~~самозакрывов~~ в ToDo и свежих journal-секциях нет. Остается только приёмка Вл
(upstream-sync merge + деплой, causal V-O9, skills-write-forbidden).

## Autonomy-тик 14:0x (06.11)

Дерево чисто; `main == origin/main == 64f13539ce` (fetch+ls-remote), непушнутого
нет. Тени сверены по ls-remote поштучно: elk/skills-write-forbidden 464e76d36f,
elk/topic-gate ce2f83975d, elk/topic-gate-inject-v02 0c285095d1,
elk/upstream-sync-20261106 5e8e71120a, more-mem-layer eb04f3a4da — все == origin;
elk/ticker-audit-fixes — предок HEAD (is-ancestor подтверждён).
Находка тика: `fleet/rebase-2710` (worktree ~/ha-upstream, дерево чисто) —
локальная ветка без origin-тени, tip 6bb04ab866 (gateway loop-level heartbeat
file) не в main (git cherry '+'). Залита молча в origin по правилу осиротевших
теней (source==dest FF); ls-remote сверен. Содержание — не деплоить, просто
сохранено.
Шлюз active с 12:14:38, за 15 мин только собственные WARNING
agent.tool_executor (execute_code-BLOCKED этого тика) — ни Traceback, ни
повторных ERROR. Матрикс-блип 13:06 (классифицирован ранее) не повторялся.
«ждёт агента»/~~самозакрывов~~ в ToDo и свежих journal-секциях нет.
Остаётся только приёмка Вл (upstream-sync merge+деплой, causal V-O9,
skills-write-forbidden).

## Autonomy-тик 14:2x (06.11)

Дерево чисто; `main == origin/main == HEAD == 88c7222a0a`, `@{u}..HEAD` пуст —
непушнутого нет. Шлюз active с 12:14:38; за 15 мин ровно 3 строки — все
собственные WARNING agent.tool_executor (execute_code-BLOCKED чужих cron-сессий:
2 мгновенных блока 14:07 + один полный тайм-аут 3602 c в 14:12). Это не
ворота-неисправность (правило тика), классифицировано, не повторяется.
jobs.json: enabled-стрейки nonzero только у известных stale-строк
daily-trace-harvest (last_run 05.11 23:30) и idle-intake-interesting
(06.11 00:30) — обе до ремонта install-key 08:58, самоочищаются следующим
запуском, не триажим.
ToDo.md не менялся (163e12d5c5 — секции приёмки нет); «ждёт агента» и
~~самозакрывов~~ нет. Остаётся только приёмка Вл (upstream-sync merge+деплой,
causal V-O9, skills-write-forbidden).

## Autonomy-тик 14:4x (06.11)

Дерево чисто; `main == origin/main == HEAD == cf8087f2de`, `@{u}..HEAD` пуст —
непушнутого нет (сверено `ls-remote`: main=cf8087f2de, fleet/upstream-sync=
a54772c334 — отдельные тени, расхождение main/fleet ожидаемо). Шлюз active; за
15 мин ровно 1 запись — не ошибка. external-workers: свежий .stderr 14:44 =
безобидный RuntimeWarning runpy, не сбой воркера. jobs.json: enabled nonzero
только известные stale-строки daily-trace-harvest/idle-intake-interesting
(запуски 23:30/00:30 — до pm-repair 08:58), следующий прогон tonight,
самоочистятся, не триажим. ToDo.md без изменений (mtime 05.11 13:42); «ждёт
агента»/~~самозакрывов~~ нет. Остаётся только приёмка Вл (upstream-sync
merge+деплой, causal V-O9, skills-write-forbidden).

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

## Autonomy-тик 14:5x (06.11)

Дерево чисто; `main == origin/main == 041eee0226` (ls-remote). Шлюз active с
12:14:38; за 15 мин нет ни Traceback, ни ERROR — только собственные WARNING
agent.tool_executor cron-сессий (14:45 heredoc-BLOCKED + jq-косяк чужой сессии,
14:56 execute_code-BLOCKED этого тика) — классифицировано ранее, не ворота.
ToDo.md (mtime 05.11 13:42): «ждёт агента» и ~~самозакрывов~~ нет.

Находка тика: 8 осиротевших локальных веток без origin-тени с типами не в main
(тот же класс, что fleet/rebase-2710 в 14:0x): keep/live-snapshot-2410
(RELEASES.md утверждал «запушена в origin», но на origin её не было — расхождение
устранено самим фактом пуша), live-snapshot-2610, controller-supervision,
feat/slash-for, feat/sqlite-checkpoint-v2, docs/fork-slash-commands,
webhook-cross-host, webhook-smoke. Залиты молча по правилу осиротевших теней
(создание тени, FF-смысла нет — веток на origin не было); ls-remote сверен по
восьми хешам (local==remote). Содержание не деплоить — просто сохранено.

Остаётся только приёмка Вл (upstream-sync merge+деплой, causal V-O9,
skills-write-forbidden).

## Autonomy-тик 15:3x (06.11)

Дерево чисто; `main == origin/main == 1aee7d052b` (ls-remote сверен). Тени
elk-arm64-timeout-headroom{,-pr}, fix/pending-review-sigil, fix/stage-dedup,
fleet/rebase-2710 — local==remote по хешам (весь «ahead» список в for-each-ref
— артефакт tracking-on-main, не непушнутая работа). Шлюз active с 12:14:38;
за 15 мин — ни одной записи юнита (0 ERROR/Traceback). ToDo.md (118 строк,
mtime 05.11 13:42): «ждёт агента» нет, `~~` — 0 вхождений; секции «ЖДЁТ
ПРИЁМКИ ВЛ» нет с правки Вл 163e12d5c5 — вскрывать нечего. Остается только
приёмка Вл (upstream-sync merge+деплой, causal V-O9, skills-write-forbidden).

## Autonomy-тик 16:0x (06.11)

Дерево чисто; `main == origin/main == 9d51fba9fc` (ls-remote сверен). Тени
elk-arm64-timeout-headroom{,-pr}, fix/pending-review-sigil, fix/stage-dedup,
fleet/rebase-2710 — local==remote по хешам («ahead» в for-each-ref — артефакт
tracking-on-main). Шлюз active с 12:14:38; за 15 мин — ровно 1 запись:
WARNING tool_executor 15:54:57 «No matches found (not an error)» — собственный
греп чужой cron-сессии, не воркер-стрейк (классифицировано в 14:2x), ни одного
Traceback/ERROR. ToDo.md (mtime 05.11 13:42): «ждёт агента» нет, `~~` — 0
вхождений; секции «ЖДЁТ ПРИЁМКИ ВЛ» нет с правки Вл 163e12d5c5 — вскрывать
нечего. Остаётся только приёмка Вл (upstream-sync merge+деплой, causal V-O9,
skills-write-forbidden).

## Тик 16:4x (06.11)
- Git: дерево чисто; main 6c3e63411a == origin/main (ls-remote). Тени сверены
  поls-remote: elk/autonomous-mode, elk/events-cron-removal,
  elk/skills-write-forbidden (464e76d36f), fleet/rebase-2710,
  fleet/upstream-sync, more-mem-layer, elk/upstream-sync-20261106 — все MATCH.
  elk/ticker-audit-fixes отсутствует на origin — предок main (merge-base
  --is-ancestor), тенить нечего.
- Шлюз 15 мин: 3 строки, 0 ERROR; Traceback внутри строки — tool_executor
  WARNING чужой сессии (execute_code), не шлюз. glm-libert api_key WARNING —
  стоячая конфиг-норма auxiliary-клиента.
- ToDo.md (118 строк): «ждёт агента» нет, `~~` — 0 вхождений; секции приёмки
  нет с 163e12d5c5 — вскрывать нечего. Память: убрана устаревшая строка
  «sync-ветка ждёт перевода чекаута/рестарта» (исполнено 06.11 12:14,
  зафиксировано второй строкой). Остаётся только приёмка Вл.

## Autonomy-тик 16:5x (06.11)

Дерево чисто; `main == origin/main == fca04fa794` (ls-remote сверен). Полная
эскалация скана теней (for-each-ref по всем refs/heads/*, не только выборочный
список прошлых тиков): 17 веток без origin-тени — все предки origin/main
(merge-base --is-ancestor), содержать нечего, тенить нечего. Единственная
реальная находка: `elk-matrix-multibot` — локальная 22bfed608f, тень на origin
отставала на dc42dd3e64; расхождение FF-совместимо, локальная вершина предок
main. Тень обновлена молча (push FF), ls-remote сверен: local==remote.
Шлюз active с 12:14:38; за 15 мин — 1 запись: WARNING tool_executor 16:41
(bad substitution в grep чужой cron-сессии), не сбой шлюза (классифицировано
в 14:2x), ни одного Traceback/ERROR. ToDo.md (mtime 05.11 13:42): «ждёт
агента» нет, `~~` — 0 вхождений; секции «ЖДЁТ ПРИЁМКИ ВЛ» нет с правки Вл
163e12d5c5 — вскрывать нечего. Остаётся только приёмка Вл (upstream-sync
merge+деплой, causal V-O9, skills-write-forbidden).

---

## Тик 17:1x (06.11)

Чисто. Полная сверка 68 локальных веток (for-each-ref, точное поsha-сравнение
с origin ls-remote): все вершины == origin-тени одного имени, ни одного
расхождения; working tree пуст, main==origin/main@f2a072cce2 (0 непушных).
f2a072cce2. Fleet-тень upstream-sync@a54772c334 — легаси-ветка реестра ToDo
(14 её коммитов не в main), содержимое ToDo.md идентично main (git diff пуст),
трогать нечего. Шлюз active с 12:14:38; за 15 мин — 2 записи: WARNING
tool_executor 17:06 (чужая cron-сессия, journalctl «No entries» exit 1),
не сбой шлюза; ноль Traceback/ERROR. ToDo.md: «ждёт агента» нет, `~~` — 0,
секции «ЖДЁТ ПРИЁМКИ ВЛ» нет с правки Вл 02.11 — вскрывать нечего. Остаётся
только приёмка Вл (upstream-sync merge+деплой, causal V-O9,
skills-write-forbidden, fork_profile).

---

## Тик 17:3x (06.11)

Чисто. Рабочее дерево пусто, HEAD==origin/main@963ba64fe9 (ls-remote сверен;
«незапушенные» из `origin/fleet/upstream-sync..HEAD` — ложный сигнал
restricted fetch refspec, легаси-флит-тень отстаёт by design). За 15 мин
журнала шлюза 4 строки — все WARNING agent.tool_executor 17:17–17:20
(чужие cron-сессии: heredoc/execute_code guard-блокировки, DIVERGED-вывод
сверки 17:1x-тика), ноль Traceback/ERROR, шлюз без рестартов с 12:14:38.
ToDo.md: «ждёт агента» нет, `~~` — 0, секции «ЖДЁТ ПРИЁМКИ ВЛ» нет с правки
Вл 02.11 — вскрывать нечего. Остаётся только приёмка Вл (upstream-sync
merge+деплой, causal V-O9, skills-write-forbidden, fork_profile).

---

## Тик 17:4x (06.11)

Чисто. Рабочее дерево пусто, HEAD==origin/main@368e264864 (ls-remote сверен).
Флит-тень a54772c334 == локальный ref, ToDo.md не менялся с 05.11 — синхрон
не требуется. За 15 мин журнала шлюза 2 строки — обе WARNING
agent.tool_executor 17:42:54 (execute_code guard-блокировка собственно
этого тика), ноль Traceback/ERROR; шлюз активен, рестартов не было
(ExecMainStartTimestamp 12:14:38). ToDo.md 118 строк: «ждёт агента» нет,
`~~` — 0, секции «ЖДЁТ ПРИЁМКИ ВЛ» нет с правки Вл 02.11 — вскрывать
нечего. Остаётся только приёмка Вл (upstream-sync merge+деплой, causal V-O9,
skills-write-forbidden, fork_profile).

---

## Тик 17:5x (06.11)

Чисто. Рабочее дерево пусто, HEAD==origin/main@3592493e37 (ls-remote сверен).
За 15 мин журнала шлюза 2 строки — обе WARNING agent.tool_executor 17:42:54
(execute_code guard-блокировка предыдущего тика), ноль Traceback/ERROR;
шлюз активен без рестартов (ExecMainStartTimestamp 12:14:38). ToDo.md:
«ждёт агента» нет, `~~` — 0, секции приёмки нет с правки Вл 02.11 —
вскрывать нечего. Остаётся только приёмка Вл (upstream-sync merge+деплой,
causal V-O9, skills-write-forbidden, fork_profile).

---

## Тик 18:1x (06.11)

Чисто. Рабочее дерево пусто, HEAD==origin/main@5083d3ed55 (ls-remote сверен,
ahead/behind 0/0); fleet-тень a54772c334==origin. За 15 мин журнала шлюза
2 строки — обе WARNING agent.tool_executor 18:05 (grep/jq-проверки предыдущего
тика), ноль Traceback/ERROR; шлюз активен без рестартов (NRestarts=0,
ExecMainStartTimestamp 12:14:38). ToDo.md (118 строк): «ждёт агента» нет,
`~~` — 0, секции приёмки нет с правки Вл 02.11 — вскрывать нечего. Остаётся
только приёмка Вл (upstream-sync merge+деплой, causal V-O9,
skills-write-forbidden, fork_profile).

---

## Тик 18:2x (06.11)

Чисто. Рабочее дерево пусто, HEAD==origin/main@deef2040ec (ls-remote сверен,
ahead 0); fleet-тень a54772c334==origin. За 15 мин журнала шлюза — ноль строк
(-- No entries), ноль Traceback/ERROR; шлюз активен без рестартов (NRestarts=0,
ExecMainStartTimestamp 12:14:38). Доп. сверка сирот: 17 локальных веток без
теней на origin — все проверены `merge-base --is-ancestor` как MERGED в
origin/main (древние 13.10–02.11), ни одной с незапушенной работой; чистить
не велено, тронуты не были. ToDo.md: «ждёт агента» — 0, `~~` — 0, секции
приёмки нет с правки Вл 02.11 — вскрывать нечего. Остаётся только приёмка Вл
(upstream-sync merge+деплой, causal V-O9, skills-write-forbidden, fork_profile).

---

## Тик 18:4x (06.11)

Чисто. Рабочее дерево пусто, HEAD==origin/main@ed7b85c2e4 (ls-remote сверен,
ahead 0 / behind 0); fleet-тень a54772c334==origin. За 15 мин журнала шлюза —
ноль строк, ноль Traceback/ERROR; шлюз активен без рестартов
(ExecMainStartTimestamp 12:14:38). Полный прогон по всем веткам: ни одной
локальной ветки с незапушенной работой относительно её настроенного upstream
(NO_UPSTREAM-список — историческое стояние, сироты сверялись тиком 18:2x как
MERGED). ToDo.md: «ждёт агента» — 0, `~~` — 0, секции приёмки нет с правки Вл
02.11 — вскрывать нечего. Остаётся только приёмка Вл (upstream-sync
merge+деплой, causal V-O9, skills-write-forbidden, fork_profile).

---

## Тик 18:5x (06.11)

Чисто. Рабочее дерево пусто, HEAD==origin/main@a419fea167 (ls-remote сверен,
ahead 0); fleet-тень a54772c334==origin (ToDo.md не менялся — синк не нужен).
Журнал шлюза за 15 мин — «-- No entries --»; юнит активен без рестартов
(ExecMainStartTimestamp 12:14:38). Журналы: все вхождения «ждёт агента» —
исторический нарратив тиков (Gate-B додрян 06.11), живых пунктов нет; ToDo.md
(mtime 05.11 13:42): «ждёт агента» 0, `~~` 0, секции приёмки нет с правки Вл
02.11 — вскрывать нечего. Остаётся только приёмка Вл (upstream-sync
merge+деплой, causal V-O9, skills-write-forbidden, fork_profile).

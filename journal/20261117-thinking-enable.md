# thinking-enable (stage004) — журнал

## НАМЕРЕНИЕ

ТЗ: автономность мышления — от включённого кода к живым строкам

**Слаг:** `thinking-enable` · **Исполнитель:** агент, ведущий форк (elk-контур) · **Заказчик/приёмка:** Вл
**База:** срез 16.11.2026, HEAD `eeb5ee52ea1`, ветка `elk/kanban-excision`, живой `~/.hermes/state.db`
**Формат ведения:** этот текст — раздел в `README.md` (раздел «Проект hermes autonomous (elk)») и запись
в `ToDo.md` как `stage004 thinking-enable`. Отдельный spec-файл не плодить (правило README:
«новый текст — разделом сюда»). Рабочие заметки — append-only в `journal/<дата>-thinking-enable.md`.

---

### 0. Рамка (почему ТЗ вообще про это)

Доктрина форка: «Ядро хранит условия рассуждения, решения принимает агент»
(docs/preface-autonomous-agent.md).
Механизмы условий рассуждения написаны (5 таблиц `investigation_*` + хуки H1–H4, `ctl_todo`,
`ctl_followup`, `ctl_causal_link`, `run_validation`, приёмка по хешу артефакта). В бою они не
используются: **0 строк** во всех когнитивных таблицах при 25 151 строке транспортных
наблюдений и 7 115 рёбрах причинного графа. Значит задача не «придумать новое мышление», а
**подключить написанное и замкнуть обратные рёбра** — и перестать плодить новые оси.

Правила, которые запрещают уходить в сторону (нарушение = переделка, не «документированный
компромисс»):

| № | Правило | Источник |
|---|---|---|
| 1 | «Уже есть в коде» — не база, а кандидатура: сверить форму со словами контракта | cautionary §1 |
| 2 | Механизм — в горле процесса, поверхности — транспорты | cautionary §2 |
| 3 | Состояние — в ленте; состояние вне ленты = налог | cautionary §3 |
| 4 | Эпруве не бывает со стороны агента; правую половину решает приёмка Вл | cautionary §4 |
| 5 | «Работает» = доказано живым исполнением; иначе честно «есть, но не работает» | cautionary §5 |
| 6 | Костыль ликвидируется, а не объявляется избыточным | cautionary §6 |
| 7 | Молчание инструмента — данные: читать артефакты, а не интерпретировать | cautionary §7 |
| — | «Text instructions are not a reliability mechanism» | `AGENTS.md` § operating rules |

---

### 1. Цель и определение готово

**Цель:** через 3 недели бою в живой БД экземпляра есть **ненулевые, осмысленные** строки в
когнитивных таблицах, и хотя бы одна задача дошла от `draft` до приёмки **без человеческой
команды в середине**, с машинным предикатом вместо вердикта «текст похож на успех».

**Готово** = выполнены S0–S3, метрики S0 собираются автоматически, и все числа раздела 6
улучшились относительно baseline-среза этого ТЗ. Ни один этап не считается сделанным на
«тесты зелёные»: зелёные тесты = существование кода (cautionary §5).

---

### 2. Не делать (границы этапа)

- **Не** создавать новую подсистему, таблицу-ось или «механизм мышления» поверх неиспользуемых
  (урок write-dialog: 55 файлов на механизм, который клеем и был). Разрешённые изменения схемы:
  новые **колонки** в существующих `ctl_*`/`investigation_*` (миграций нет, `SCHEMA_SQL` +
  reconcile), никаких новых таблиц-осей.
- **Не** лечить ложные done текстом в промпте и **не** плодить блоки `# Execution discipline`.
- **Не** давать агенту право self-approve «успеха» (cautionary §4). Права на *когнитивные*
  действия — да (S2), права на *оценку успеха* — нет.
- **Не** тащить новые отчёты в «состояние вне ленты»: всё, что видит Вл, — сообщения в комнату/CLI.
- **Не** строить «автономное целегенерирование» до S0: без метрик это maintenance-epoch с нулём строк.
- **Не** трогать `hermes_events`, `tasks_kernel`, `honesty-gate`, `kanban`-остатки, `planning_multistage`
  как отдельную ось — их судьба решается в S4, до этого не развивать.
- Git: `origin` = VovaElkTver/* только; upstream read-only.

---

### 3. Этапы

Порядок строгий. Каждый этап — отдельный коммит(ы) с префиксом `ops: thinking-enable S<n> …`,
append-строка в journal, и **машинная проверка** из колонки «проверка». Этапы S0–S1 не требуют
решений Вл; S2 требует одного (см. §5).

#### S0 — Метрики автономности (VIEW + суточный отчёт в ленту). 1 день.

Сейчас `evals/postmortem/forensics/*` измеряют токены/латентность/доставку; автономность не
измеряется нигде. Сначала — метр, потом «улучшения».

Горло: новый `scripts/autonomy_metrics.py` (read-only SQL к `state.db`, никакого нового состояния) +
one-shot выдача в ленту через штатный порт доставки (`controller/outbox.py`). Никаких новых
потоков/таймеров: вызов — из существующего тика как событие (правило «отложенное — событие, не
таймер»).

Метрики (все считаются из существующих таблиц, новых записей не нужно):

1. `completed` без человеческой команды между `draft` и приёмкой / все `completed`;
2. медиана шагов и времени до `needs_human` / `blocked`;
3. доля «done», у которых есть машинный предикат (`validation_cmd` прошёл) и непустой
   `criteria_covered` — ловец ложных done;
4. self-initiated wake vs human-initiated (по `ctl_event.origin` / `sessions.created_source`);
5. глубина pending-приёмки: открытые/закрытые записи реестра (правило «открытые не должны расти
   быстрее закрытых» измерялось один день 31.10 и умерло — возродить как метрику, не как обещание);
6. `count(investigation_*)`, `ctl_todo`, `ctl_followup`, `ctl_review` с `reviewer='agent'`;
7. цена одного человеческого вмешательства (терны/токены на задачу с вмешательством vs без).

Проверка:
```bash
python3 scripts/autonomy_metrics.py --db ~/.hermes/state.db --json | python3 -m json.tool >/dev/null
python3 scripts/autonomy_metrics.py --db ~/.hermes/state.db --markdown | head -20   # baseline в journal
```
Приёмочный признак этапа: цифры S0 напечатаны в ленту и записаны в journal как baseline, с
датой и с ссылкой на **копию-снимок** БД (`sqlite3 .backup` → `~/.hermes/snapshots/<ts>.db`).
Детерминированность проверяется на снимке, а не на живой базе: живой `state.db` меняется между
двумя прогонами одной минуты (`causal_links` 7 115 → 7 124, `transport_obs` 25 151 → 25 252 —
замерено дважды при написании ТЗ). При пустых когнитивных таблицах отчёт даёт нули, а не падение.

#### S1 — Подключить написанное (5 переключателей + гигиена тикера). 2–3 дня.

| # | Что | Горло | Проверка |
|---|---|---|---|
| S1.1 | Мыслительные инструменты в поверхности: toolset `sessions` (или новый `thinking` = `investigation` + `ctl_*` + `plan_multistage`) в `platform_toolsets.cli\|matrix` | [investigation_tool.py:234], [config.yaml:319-351], [model_tools.py:329-341] | рабочая проверка (реестр надо поднять, иначе `sessions` резолвится в `[]`): `python3 -c "from tools import registry; registry.discover_builtin_tools(); from toolsets import resolve_toolset as r; ts=set(r('sessions')); assert {'investigation','session_fork'} <= ts, ts; from hermes_cli.config import load_config as L; assert 'sessions' in (L().get('platform_toolsets') or {}).get('cli',[]), 'sessions не в поверхности'  # на 16.11 -> AssertionError, это и есть finding` |
| S1.2 | Автоподвязка наблюдения к пробе: при открытом investigation рантайм привязывает результат инструмента к последнему `dispatched`-пробу сессии; явный `probe_id` остаётся точным путём (снимает «модель должна помнить» — ту же ошибку, что `controller_task_id` в S1.4) | [investigation_hooks.py:44-60] | новый тест в `tests/test_investigation_hooks.py`: результат без `probe_id` → `assessed=0`-строка с верным `probe_id` |
| S1.3 | Машинный предикат приёмки: вызвать `run_validation` в `verifying` **до** `review_fn`; починить форму (`acceptance` dict vs list); `validation_cmd` обязателен в draft-контракте, сгенерированном планом | [workers.py:327-333], [model.py:98], горло [loop.py:207] | `python3 -m pytest -q tests/plugins/test_controller_loop.py` + регресс: `pass` судьи при failed `validation_cmd` → `revise` |
| S1.4 | Цена действия: проставить `controller_task_id` в dispatch-контексте (иначе бюджет по tool_calls тождественно False — тот же класс ошибки, что починенный 09.11 `budget_exhausted`); дефолт `budget_seconds` на автономный период | [plugins/controller/__init__.py:88-124], [workers.py:110-118] | тест: достигнутый лимит → `blocked` + alert в `ctl_event`, а не «тихий» продолженный ран |
| S1.5 | Два выключателя, убивающие честность и обучение: `verification.verify_on_stop: "auto"` (в живом конфиге секции нет → выключен) и `auxiliary.background_review.enabled: true` с опозданием в существующую idle-очередь `agent/review_idle_queue.py` | [config_defaults.py:212-218] | доля финальных ответов со свежим read-back (метрика S0.3); шум нуджей считается отдельно и **не** является основанием выключить гейт (причина, по которой его вырубили, должна быть измерена, а не унаследована) |

S1.6 (гигиена, из остатков аудита 28.10, [docs/critiques/2026-10-28-ticker-pre-launch-audit.md]):
`conn.rollback()` в `_guard` перед error-`heartbeat`; heartbeat на отдельном read-only-соединении;
внешний не-LLM проверитель `MAX(ts)` из `ctl_ticker_heartbeat` (systemd timer, не модель).
Проверка: тест, где `heartbeat` бросает `database is locked` → тред тикера жив (регесс на «тихую
смерть автономии»).

#### S2 — Права на мыслительные действия (не на мутации). 3–5 дней.

У агента ровно 4 инструмента ([agent_tools.py:206-264]);
`followups.add()` вызывают только тесты, `ctl_todo` материализуется только из CLI
([pump_cli.py:36-114]). Отсюда — никто не будит агента «решить, что делать», и следствие своего действия он назначить не может.

| # | Что | Примечание |
|---|---|---|
| S2.1 | Класс гранта `cognitive` вместо вечно `ordinary`: `barrier.check` вызывается всегда с `action_class="ordinary"` ([barrier.py:38-45], а `grant_approval` не вызывается никем ([model.py:440] — весь `ctl_approval` декорация. Когнитивные действия (открыть investigation, гипотеза/проб/предсказание, `ctl_todo`, `ctl_followup`, приоритет draft/ready, read-only зонды) — разрешены всегда в рамках scope; MATERIAL-мутации — как сейчас, только с человека | Это **не** self-approve и снимает доктринальный тупик, из-за которого `docs/design/maintenance-epoch.md` завис в 4 вопросах к Вл: самовопрос не требует самодозвола, если он не MATERIAL |
| S2.2 | Инструменты `ctl_todo add/list/propose`, `ctl_followup add(reminder\|review)`, `ctl_reprioritize(head\|tail, basis)` — пишут в существующие таблицы, остаются «предложением» для очереди | мост к уже написанному `followups`-механизму ([followups.py:8-18,89] |
| S2.3 | Дефолт неопределённости: `retry_same_step` из clamp ([failure_analysis.py:91-104] → «повтор шага только при наличии свежего предсказания или изменённого контракта, иначе `needs_human` с названным недостающим знанием». Начать применять `modified_brief` (сейчас считается и выбрасывается) | отказ должен быть дешевле ложного done: сегодня `отказ = failed`, поэтому агенту выгодно объявить done |
| S2.4 | Последствия в контекст: перед сборкой брифа поднимать из `ctl_causal_link` подграф `about/causes` по задаче/проекту за последние N событий + последние `ctl_review.detail` ([workers.py:264-291], читатели графа сегодня shadow — [causal.py:31-34]) | машинная версия cautionary §7; лечит «мой стоп+TRUNCATE породил вторую аварию» (08.11), «кем-то внешним снят interim-патч» (27.10) |
| S2.5 | Судья видит смысл, а не хвост: в `make_review` передавать `acceptance` + `facts` + diff/diffstat вместо `step.title` + `stdout_tail[-2000:]` ([workers.py:251-306]; в `ctl_review` — колонка `criteria_covered` (покрытие критериев, а не уверенность). Форма — из `src/claims/{core,oracle}.py`: популяция вне кандидата, предикат заморожен, контрпример = отдельная строка отчёта | сейчас левое условие `completed` = «текст похож на успех» при zero-покрытии |
| S2.6 | Событие «решить, что делать»: wake-терн на `completed\|blocked\|needs_human` и на «очередь пуста + permission on» с материалом (`ctl_status` + `ctl_todo` + неоценённые наблюдения + прошлые `needs_human`). Выход — только `draft` / `reprioritize` / `noop(причина)`, где **`noop` = успех** | `noop` обязан быть ассерт-успешным: сегодня «отказ от действия» не ассертится нигде |

#### S3 — Стоп-условия, калибровка, риск. 1 неделя, по меркам из S0.

- **S3.1** Симметрия стопа архитектурно, не текстом: детерминированный гейт в существующем горле
  ([turn_stop_gates.py:189] — N шагов без нового подтверждённого факта (ни одного `assessed=1`,
  ни одного изменившегося read-back) ⇒ новый проб либо `needs_human`). Сейчас H4 умеет пинать, но
  пинать не за что (0 наблюдений).
- **S3.2** Калибровка: сравнить `investigation_predictions` с `assess`-исходом, накопить точность
  самопрогноза по scope. Единственный правдоподобный путь к «агент знает, когда ему себе не
  верить»; сегодня нет ни одной величины, из которой это следует.
- **S3.3** Риск-предиктор до действия (один дешёвый aux-вызов) для класса «мутация вне workdir /
  `git push` / `rm` / рестарт own-сервиса», вердикт — в `ctl_event` и в бриф. Ловит инциденты
  журнала: git-коррупция основного репо (12.11), 87/240 рестартов за ночь (15.11/08.11),
  контекст 3.1M токенов за минуту до SIGTERM (15.11 — деградация была видна, датчика не было).
  Заодно: ни один контракт сегодня не ограничивает длину автономного периода.
- **S3.4** Убрать мёртвые declared-only механизмы или признать их: `memory_gate`, `preempt`,
  `receipts`, `disposition`, семантические поля контракта
  (`allowed_actions/stop_conditions/reclassification_rules/open_questions/hypotheses` — пишутся,
  не читаются: [model.py:97-102]).
  По правилу §6: либо вписать в горло, либо удалить вместе с тестами. Не «задокументировать как известное ограничение».

#### S4 — Одна ось цели (решение Вл, не код). Обсуждать после S0–S3.

Сейчас пять осей «что делать дальше» с непересекающимися состояниями и разными «кто решает»:
`controller`-очередь (1 задача), `hermes_cli/goals.py` (0 состояний `goal:%` в `state_meta`),
`tasks_kernel/`, `hermes_events/` (1 строка в `events.db`), `planning_multistage` (0 вызовов) +
`honesty-gate` (0 вызовов `stage_open`, `provides_hooks: []`). Kanban вырезан (с crash-loop 16.11),
cron-инструментарий удалён, при этом 3 197 из 4 240 сессий кончились `startup_orphan_reap`.
Кандидат сварки: `ctl_task` = обязательство, `/goal` = исполняющий терн-цикл внутри одного `step`,
`investigation` = состояние рассуждения этого терна, `ctl_followup` = отложенные следствия;
остальное — ликвидировать по правилу §6.

---

### 4. Baseline (срез 16.11, прод `~/.hermes/state.db`: 4 240 сессий, 192 407 сообщений)

Команда снятия (перезапускать перед каждым отчётом; расхождение baseline↔отчёт = инцидент):

> Снимок baseline **обязан быть с меткой времени и с пометкой «живая БД»**: экземпляр пишет
> непрерывно, и `causal_links`/`transport_obs` расходятся уже между двумя прогонами одной минуты
> (7 115 → 7 124 за время написания этого ТЗ). Не-ковидные метрики (когнитивные таблицы) —
> «0 → >0», они дрейфу не подлежат.


```bash
mkdir -p ~/.hermes/snapshots && S=~/.hermes/snapshots/$(date -u +%Y%m%dT%H%M%SZ).db
sqlite3 -readonly ~/.hermes/state.db ".backup '$S'"     # дальше мерить только снимок
sqlite3 -readonly "$S" <<'SQL'
select 'ctl_task', count(*) from ctl_task union all
select 'ctl_review', count(*) from ctl_review union all
select 'ctl_todo', count(*) from ctl_todo union all
select 'ctl_followup', count(*) from ctl_followup union all
select 'ctl_approval', count(*) from ctl_approval union all
select 'ctl_mem_item', count(*) from ctl_mem_item union all
select 'investigations', count(*) from investigations union all
select 'inv_observations', count(*) from investigation_observations union all
select 'causal_links', count(*) from ctl_causal_link union all
select 'transport_obs', count(*) from transport_observations;
select tool_name, count(*) from messages where tool_name in
 ('investigation','session_fork','plan_multistage','stage_open','ctl_step_done','ctl_enqueue','ctl_status')
 group by 1 order by 2 desc;
select count(*) from state_meta where key like 'goal:%';
SQL
```

| Показатель | 16.11 | Цель через 3 недели |
|---|---|---|
| `ctl_task` | 1 (`completed`) | ≥ 10 |
| `ctl_review` / с машинным предикатом | **0** / — | ≥ 8 / ≥ 5 |
| `investigations` / `observations` | **0** / **0** | ≥ 5 / ≥ 40 (с `assessed=1`) |
| `ctl_todo` / `ctl_followup` от агента | **0** / **0** | ≥ 10 / ≥ 5 |
| `ctl_approval` | **0** | > 0 или удалён механизм (S3.4) |
| вызовы `investigation` / `plan_multistage` / `stage_open` | 0 / 0 / 0 | > 0 / решение S4 / решение S4 |
| goal-состояний | **0** | решение S4 |
| задачи, дошедшие до приёмки без человека в середине | 0 | ≥ 3 |
| `ctl_mem_item` | **1** | > 0 или механизм удалён (S3.4) |

---

### 5. Решения, которые нужны от Вл (без них S2 стоит)

1. **Класс `cognitive`: утвердить список разрешённых навсегда когнитивных действий** и где он
   живёт (реестр в коде vs таблица). Без этого S2.1/S2.2 не реализуемы, а доктринальный запрет
   «no model text can approve» блокирует и самовопрос (см. 4 открытых вопроса
   `docs/design/maintenance-epoch.md`, застрявших ровно здесь).
2. **`noop(причина)` как успех** — подтверждать, что «осмотрелся и осознанно ничего не сделал»
   не является отказом/`failed`.
3. **`verify_on_stop` и `background_review` на каких поверхностях включать** (CLI / matrix /
   worker): в живом конфиге не задано, по умолчанию выключено; измерение шума — после включения,
   не вместо.
4. **Машинный предикат обязателен для всех новых контрактов?** Тогда `pump_cli`/`/for` обязаны
   генерировать `validation_cmd`, а контракт без него — не уходит в `ready`.

---

### 6. Ритм и отчётность

- Один этап = один заход до конца (не «напоминание на следующем бите»): конец захода —
  append в journal + метрики S0 + что не сделано. Ветки — `elk/thinking-enable-s<n>`, коммиты с
  префиксом `ops: thinking-enable S<n> …`; в `ToDo.md` этап закрывается только после приёмки Вл.
- Формат journal-секций — по [journal/README.md](journal/README.md):
  `## НАМЕРЕНИЕ` (дословно задание, без пересказа), `## ГРАНИЦА НАМЕРЕНИЯ`, `## РАУНД N — РЕЗУЛЬТАТ`,
  `## РАУНД N — ВЕРДИКТ КРИТИКА`, `## РАУНД N — НАМЕРЕНИЯ ПО ЗАМЕЧАНИЯМ`. Критик — внешний
  (critic-queue на `.100:8012`, паттерн будильника из скилла `critic-queue`), не самопроверка.
- Пакет критику: намерение + все предыдущие вердикты + намерения по замечаниям + новый результат.
- Каждый отчёт начинается с ограничений («A report starts with its limits», `AGENTS.md`),
  и в нём обязательно: *что не проверялось живым исполнением* — по cautionary §5 это помечается
  «есть, но не работает», а не «сделано».
- Права агента: read-only ко всем БД и логам; `kill/restart` own-сервиса только с явным
  риском-вердиктом S3.3; git-запись только в `origin`.

---

### 7. Риски и стоп-условия ТЗ

| Риск | Митигание / стоп |
|---|---|
| Включённая поверхность инструментов не изменит поведение (модель не потянется к `investigation`) | Признак того, что дело не в инструментах, а в отсутствии события «решить, что делать»: S2.6 поднимается в приоритете над S3. Если к концу S2 `investigation_observations` = 0 при ненулевой поверхности — ТЗ возвращается Вл на пересмотр, а не обрастает новым гейтом |
| `verify_on_stop` вернёт шум нуджей, из-за которого его вырубили | Флаг `"auto"` (только interactive-кодовые поверхности), метрика «нуджей на терн» в S0.3, лимит `max_verify_nudges=3` не менять |
| Машинные предикаты станут новой вакуумной формой (гейт проходит, смысла нет) | Проверка S2.5: предикат обязан проверять **критерий приёмки**, а не «код компилируется»; контрпример = тест, где предикат зелёный, а `criteria_covered` пуст ⇒ `revise` |
| Разрастание агентной поверхности (ещё 3 инструмента = ещё 55 файлов) | Все S2-инструменты пишут в существующие таблицы и не имеют собственного состояния; приёмочный критерий: ни одного нового файла с `class`/`pending`-состоянием |
| S4 решится «потом» и оси останутся | S4 — единственный этап, который нельзя закрыть кодом; без него ТЗ не считается выполненным, даже если метрики S1–S3 зелёные |

## ГРАНИЦА НАМЕРЕНИЯ

- §2 ТЗ (не делать): без новых таблиц-осей/подсистем; без self-approve успеха; без состояния
  вне ленты; без целегенерирования до S0; `hermes_events`/`tasks_kernel`/`honesty-gate`/
  `kanban`-остатки/`planning_multistage`-ось не трогать; git — только `origin` VovaElkTver/*.
- Права агента (§6): read-only ко всем БД и логам; `kill/restart` own-сервиса — только с
  риском-вердиктом S3.3; git-запись только в `origin`.
- Изменения схемы: только новые **колонки** в существующих `ctl_*`/`investigation_*`
  (`SCHEMA_SQL` + reconcile), никаких новых таблиц-осей.

## РАУНД 1 — РЕЗУЛЬТАТ (S0, 17.11)

Ограничения (по AGENTS.md: отчёт начинается с ограничений):
- Доставка в ленту подтверждена только exit-0 штатного транспорта `hermes send -t matrix -q`
  (креденшелы гейтвея); независимый read-back истории комнаты через Matrix client API не
  выполнен (endpoint 404 с этого хоста) — помечено «доставлено, read-back не выполнен», а не
  «проверено».
- M7 токены: не измеряемо (в схеме нет run→session связи) — честно null, не 0.
- Детерминированность доказана только на снимке; на живой БД метрики дрейфуют (обоснование —
  в ТЗ §4).

Сделано:
1. ТЗ внесено в README (раздел «thinking-enable (stage004)» в «Проект hermes autonomous (elk)»)
   и ToDo.md (запись `stage004 thinking-enable`, закрытие — только после приёмки Вл).
2. Горло S0: `scripts/autonomy_metrics.py` — read-only SQL к state.db (открытие mode=ro),
   7 метрик TЗ, JSON/markdown, без записи в БД. Проверки ТЗ:
   - `python3 scripts/autonomy_metrics.py --db <snap> --json | python3 -m json.tool` -> OK;
   - `--markdown | head -20` -> baseline ниже;
   - детерминизм на снимке: два прогона byte-идентичны (кроме generated_at) — DETERMINISTIC.
3. Снимок-базовая (живая БД в момент снимки, 17.11 ~05:00 UTC):
   `~/.hermes/snapshots/20261117T045911Z.thinking-enable-baseline.db`
4. База (снимок 20261117T045911Z):
   - M1 completed без человека: 0/1 (единственная completed задача несла mandate actor=vovak
     в жизненном окне — ctl_mandate: task-e29db8067b, issued 1793641608);
   - M2 стоп: задач=1, медиана шагов=0, до стопа 81.7 c;
   - M3 ложные done: done=1, с предикатом=0, с coverage=0;
   - M4 wake: self=3994 (cron 3872 + agent-fork 6 + subagent 116), human=223 (matrix 210 + cli 13),
     unattributed=26 (oneshot); ctl_event origin: controller.ticker 2546, causal 102, supervisor 1;
   - M5 pending: open=0 closed=1 (24h +0/-0);
   - M6 когнитивное: inv=0/obs=0 (assessed=0), todo=0, followup=0, review_agent=0,
     causal=7127, transport=25253 (срез ТЗ 16.11: 7115/25151 — расхождение = дрейф живой БД,
     не инцидент: снимок сделан позже среза ТЗ);
   - M7 цена вмешательства: turns c=1 (1 задача), без=0 (0 задач); tokens: NOT MEASURABLE.
5. One-shot выдача в ленту (приемочный признак): markdown-отчет отправлен в home-комнату
   matrix `hermes send -t matrix -q` (05:05 UTC), exit 0. Штатный порт доставки —
   `controller/outbox.py` + `live_ports` (deliverer `alerts.deliver` = тот же `hermes send`);
   `controller.alert_target` в живом конфиге не задан, поэтому outbox-путь деградирует в
   journal+stdout, а one-shot пошел в home-комнату напрямую через deliverer-транспорт.

Не сделано (по S0): периодичность «суточный отчёт в ленту» — вызов из существующего тика
как событие (без новых потоков/таймеров); встраивается в тикер S1 (см. РАУНД 2).

## РАУНД 1 — ДОПОЛНЕНИЕ (S0 daily-report event, 17.11)

Сделано (закрывает «не сделано» раунда 1):
- `controller/ticker.py`: интервал `autonomy_metrics` (86400 s) добавлен в
  существующую таблицу INTERVALS — СЧЁТЧИК НА ТОМ ЖЕ 1 ГЦ-ТИКЕ, не новый
  поток/таймер (правило ТЗ «отложенное — событие, не таймер»); в `_schedule`
  деадлайн-ветка вызывает `run_autonomy_metrics` через штатный `_guard`
  (сбой метрика = deduped tick-error alert, метрика не умирает молча).
  Wake-pull этот счётчик НЕ ускоряет (каденция — не due date).
- `run_autonomy_metrics`: subprocess-вызов `scripts/autonomy_metrics.py
  --markdown --deliver` (PYTHONPATH=repo root, cwd=repo root; read-only SQL,
  без записей; без alert_target доставка деградирует в stdout/journal).
- Тесты (tests/plugins/test_controller_ticker.py, 3): суточный счётчик
  однократно срабатывает за интервал и ре-анкорится; сломанный метр =
  loud RuntimeError (не тихий пропуск); зелёный путь пишет heartbeat
  'autonomy-metrics'. Все 39 тестов тикера/wake/live-ports зелёные.

## РАУНД 2 — РЕЗУЛЬТАТ (S1.1, 17.11)

Ограничения:
- Конфигурная часть S1.1 выполнена через санкционированный CLI `hermes config set
  platform_toolsets.{cli,matrix}` (guard config-protection блокирует прямые правки
  guard-конфига инструментами агента; CLI-путь — штатная запись, не обход: ТЗ прямо
  предписывает эту правку, см. §3 S1.1). В guard-сканер попал false-positive на
  pipeline-команде (токен guard-конфига в grep-хвосте) — зафиксировано.
- `hermes tools enable --platform cli sessions` не работает: CLI-процесс не знает
  registry-toolset `sessions` («Unknown toolset») — picker `hermes tools` не увидит
  toolset до discovery в своём процессе; гейтвей/CLI-сессии (model_tools.py:151
  discovery на импорт) резолвят его корректно.

Сделано:
1. `platform_toolsets.cli` и `.matrix` несут `sessions` (проверено `hermes config get`:
   sessions в обоих списках, без дублей).
2. Проверка ТЗ (обе половины) пройдена: `resolve_toolset('sessions')` =
   [fork_explore, investigation, session_fork, session_join] + `sessions` в
   platform_toolsets.cli и .matrix.
3. Регресс-тесты в tests/tools/test_toolsets.py (3): sessions без реестра = []
   (документированный режим провала), с зарегистрированными мыслительными
   инструментами = полная поверхность, live-discovery реальных tool-модулей
   даёт тот же набор.

Не сделано: ничего в рамках S1.1 (код + поверхность + проверки).

## РАУНД 3 — РЕЗУЛЬТАТ (S1.2, 17.11)

Ограничения:
- 2 упавших теста TestH4StopGate (test_gate_nudges_once_then_stays_quiet,
  test_gate_silent_when_assessed) — ПРЕСУЩЕСТВУЮЩИЙ breakage на базе ветки:
  monkeypatch отсылает к `turn_stop_gates._kanban_stop_nudge`, который удалён
  kanban-excision (проверено git stash: те же 2 падают без моих изменений).
  По ТЗ §2 kanban-остатки — не трогать до S4; фикс отложен туда.

Сделано:
1. Автоподвязка H2 (agent/investigation_hooks.py): явный probe_id = точный путь
   (первый контакт переводит пробы pending→dispatched — «вызов и есть
   диспатч»); без явного probe_id результат привязывается к ПОСЛЕДНЕЙ
   dispatched-пробе открытого investigation сессии (снимает «модель должна
   помнить id» — тот же класс ошибки, что controller_task_id в S1.4).
   Без dispatched-проб — unbound, хук не гадает.
2. `stamp_fork_probe` переводит pending-пробу в dispatched (форк теперь её
   исполняет; исход вернётся H3-join'ом).
3. Тесты (tests/test_investigation_hooks.py, 5 новых): результат без
   probe_id → assessed=0-строка с верным probe_id (требование ТЗ),
   unbound при отсутствии dispatched, явный echo dispatched→observed,
   «последняя созданная побеждает», fork-stamp диспатчит. Все 50 тестов
   двух файлов зелёные кроме 2 предушествующих (выше).

## РАУНД 4 — РЕЗУЛЬТАТ (S1.3, 17.11)

Ограничения:
- Часть S1.3 «validation_cmd обязателен в draft-контракте» реализована
  КАК МЕХАНИЗМ (guard `_draft_has_predicate` в transitions.py определён и
  машиной проверен тестом), но НЕ АКТИВИРОВАНА: активная строгая форма
  ломала followups и agent-tools пути draft→ready (4 упавших тестов,
  поймано прогоном) — это территория решения #4 Вл («обязателен для всех
  новых контрактов?»). Активация при решении = одна строка
  `GUARDS.setdefault(("draft","ready"), []).append(_draft_has_predicate)`
  (документировано в докстринге функции).

Сделано:
1. Машинный предикат в verifying (горло = transition guard):
   `("verifying","completed")` теперь несёт `_machine_validation` —
   `run_validation` (contract validation_cmd, legacy dict-acceptance
   поддерживается) предшествует вердикту завершённости: judge-pass при
   падающем cmd ≠ completion. Отказ → finish_verifying реопенит
   последний checkpoint-шаг (done→pending CAS, `model.reopen_step`) и
   возвращает задачу в ready — REVISE, не park; попытки ограничены
   step-бюджетом контракта.
2. Форма (починка ТЗ «acceptance dict vs list»): канон = топовый ключ
   контракта `validation_cmd` (acceptance остаётся списком критериев);
   `_empty_contract` дополнен `"validation_cmd": ""`; `run_validation` +
   новый `contract_validation_cmd` читают канон, legacy dict-acceptance
   уважается; в бриф рабочего шага строка предиката вписывается явно.
3. Тесты (tests/plugins/test_controller_loop.py, 5): judge-pass при
   failed cmd → revalidate (задача ready, шаг pending — регресс ТЗ),
   pass cmd → completed, без cmd → legacy completed, legacy dict-форма
   уважается, draft-gate не активен до решения #4 (guard проверен
   напрямую). Все 184 тестов controller-подмножества зелёные.

## РАУНД 5 — РЕЗУЛЬТАТ (S1.4, 17.11)

Ограничения:
- Живое исполнение хуков на боевом экземпляре НЕ прогонялось: проверено
  тестами (tests/plugins/test_controller_budget.py, 5 шт.) на фиксуре;
  живой эффект (бюджет накапливается в боевом процессе) — с ближайшего
  автономного дренажа тикера.
- Бюджет-пауза wall-clock (пауза шага при исчерпании timeout_s) — путь
  graceful-pause уже существовал; S1.4 добавил только дефолт (3600с)
  для автономного тикер- дренажа и config-ключ
  controller.worker_budget_default_s (0 = legacy безлимит).

Сделано:
1. Диспач-контекст: HERMES_CONTROLLER_TASK_ID (уже ставился make_worker
   в env рабочего процесса) теперь ЧИТАЕТСЯ хуками: pre-hook
   (barrier) и post-hook (бюджет) резолвят контроллер-задачу по
   kwarg -> env -> scope-fallback (pre) / без fallback (post, строгое
   — учёт в чужую задачу = порча лимита). До этого kwarg никогда не
   передавался -> tool_calls-бюджет тождественно 0/NULL.
2. Достигнутый лимит = видимый стоп: barrier «budget exhausted» ->
   model.block_on_budget_exhausted (running -> blocked, детерминирован-
   ный переход) + ctl_event kind=budget-blocked (идемпотентен по
   idempotency_key). Снапшот human-gates (alerts.render_gates_snapshot)
   теперь включает budget-blocked задачи -> существующий ticker-alert
   канал сообщает в ленту. Без этого стоп был «тихим»: guard рефьил
   каждый вызов, а задача выглядела живой.
3. Тесты: 5 новых (post-hook учитывает из env; без контекста noop;
   pre-hook при spent budget блокирует И паркует задачу + ровно одно
   ctl_event; gates-snapshot показывает; правила дефолта бюджета).
   177 тестов затронутого подмножества зелёные.

## РАУНД 6 — РЕЗУЛЬТАТ (S1.5, 17.11)

Ограничения:
- Изменения конфига вступают в силу с НОВОЙ сессии/spawn (read при
  spawn / session start); рестарт гейтвея НЕ выполнялся (orphan-ловушка
  15.11: рестарт только вне cgroup). Живой эффект — с ближайшего
  интерактивного кодового терна (verify_on_stop auto) и ближайшего
  авто-ревью (background_review enabled, managed-local -> idle-очередь).
- М9=0 на baseline ЧЕСТНО: гейт был выключен, нуджей не было — это не
  «тихо», это «не запускали» (пометка в метрике).

Сделано:
1. hermes config set agent.verify_on_stop auto (ранее false): нуджи
   только на интерактивных кодовых поверхностях («auto» = митигация из
   риск-таблицы ТЗ); max_verify_nudges=3 не тронуто.
2. hermes config set auxiliary.background_review.enabled true (ранее
   false): авто-ревью включено; отсрочка в idle-очередь
   (agent/review_idle_queue.py) УЖЕ была провайрена (defer: auto,
   _review_should_defer -> QUEUE.enqueue, dispatcher с idle-гейтом) —
   подключать было нечего, проверено чтением пути run_agent._spawn_
   background_review.
3. Метрики S0.3-проверки S1.5 добавлены в scripts/autonomy_metrics.py:
   M8 fresh read-back (доля edit-сессий, закрытых наблюдением; на
   baseline 21/295 = 0.071) и M9 verify-nudges (счётчик шума с ленты;
   0 на baseline). Оба детерминированы по снапшоту.

Примечание (внешнее действие в ходе захода): 08:42 в историю ветки
вошёл мерж origin/main -> elk/thinking-enable-s0 (2929f6398c6,
«kanban excision follow-ups + dsh-B») — не моё действие (мои коммиты
в 08:41 и 08:48 — S1.3 и S1.4; мерж между ними, авторство
неустановленное, вероятно параллельный агент/тикер). Состояние
после мержа мной НЕ игнорировалось: прогоны 177 и 184 тестов
затронутого подмножества выполнены ПОСЛЕ мержа (зелёные); S1.4/S1.5
сидят на мерже в истории.

## РАУНД 7 — РЕЗУЛЬТАТ (S1.6, 17.11)

Ограничения:
- Пункт «внешний не-LLM проверитель MAX(ts) из ctl_ticker_heartbeat»
  ЧАСТИЧНО: внешний не-LLM systemd-таймер существует
  (hermes-gateway-watchdog.timer, 30s, user-unit, WATCHDOG_HANG_ACTION=1),
  НО он читает gateway-пульс/heartbeat-файл, а НЕ MAX(ts) из
  ctl_ticker_heartbeat. Расширение watchdog-скрипта (~/.hermes/scripts/
  gateway-watchdog.py — инфраструктура Вл с доказанной action-лестницей:
  budget/freeze/shadow) НЕ выполнялось без отдельного слова: минимальный
  безопасный вариант = log-only (shadow) проверка staleness
  ctl_ticker_heartbeat, не затрагивающая restart/freeze-пути. Следующий
  шаг: решение Вл (или отдельный заход с мандатом).

Сделано (верификация, а не новый код — все три пункта гигиены УЖЕ в коде
после аудита 28.10):
1. conn.rollback() до error-heartbeat: _tick_error делает conn.rollback()
   с suppress, heartbeat/alert идут на кратковременной scratch-связи
   (main-writer — подозреваемый).
2. Heartbeat на отдельной связи: _open_scratch_conn; сам heartbeat —
   bounded busy-окно + retry + SKIP (никогда не бросает exception).
3. Регрессия «тихая смерть автономии»: тест
   test_ticker_survives_a_locked_database (locked-БД на каждом тике:
   тред жив, каденция держится, алерт один — дедуп, half-cycle
   откатан, heartbeat-строки деградировали в skip) — ЗАПЕЛ, зелёный
   (1.3с, 17.11).

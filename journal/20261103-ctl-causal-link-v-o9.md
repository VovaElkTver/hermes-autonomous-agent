# 2026-11-03 — ctl_causal_link: каузальный граф в state.db (V-O9 / stage a–d)

ТЗ: prompt-for-rtx5090-causal-links.md (Вл, brainstorm hermes-agency 03.11),
requirements.md §V-O9 + уточнение C3 «лента = граф». Исполнено stages a–d одним
заходом; формат — shadow (пишем всегда, старые читатели не тронуты).

## Сверка ТЗ с реальным кодом (расхождения — важная часть)

1. **`messages.in_reply_to` в этой схеме НЕ существует** (§2.2/§3 ТЗ называют её
   донором миграции). Фактические носители reply-to: `event_observations.relation`
   (JSON c `m.in_reply_to`/`m.thread`), исходящие `reply_to_message_id` живут в
   metadata платформ и в ленту state.db не пишутся. Мигрируем из relation JSON.
2. **Единого пространства event_id нет** (ctl_event TEXT uid, messages INTEGER,
   Matrix event_id, sessions id). Введены namespaced концы: `ctl: task: rev: mem:
   todo: br: sess: obs:`; писатель резолвит существование и ts по домену.
3. **Прямое merge-ребро br→sess:parent закрывало бы цикл** против causes-ребра
   форка (ТЗ §2.2 п.3 просит «между ветками» буквально). Решение: join — событие
   сходимости (ctl_event kind=branch_joined, idempotency_key `branch_join:<br>`),
   на него идут merge-ребро от поглощённой ветки и causes от поглотившей ленты.
   DAG сохранён, смысл ТЗ не сломан.
4. **ts(to) ≥ ts(from) для reply инвертируется на живой выгрузке**: штампы
   event_observations — время ПРИЁМА sync-пакета, родитель треды может быть
   вычитан позже ребёнка (35% многоленточных случаев по пробегу: 13 из 4487
   reply-пар инвертированы). Наследование букве §2.4 резало бы эти рёбра
   навсегда. Принято исключение kind=reply из порядка ts (цикл-охрана остаётся);
   узлы sess: переведены на immutability (started_at, не last_activity_at) —
   с ним 12 из 15 исторических форков мигрировали, раньше отваливались.
   **Это отклонение от ТЗ §2.4 — на ратификацию Вл.**

## Что сделано

- SCHEMA_SQL (hermes_state_common.py): ctl_causal_link + UNIQUE(from,to,kind) +
  3 индекса. Drift-проба подхватывает декларацию автоматически.
- controller/causal.py: контрактный писатель `link` (cycle/self-loop/dangling/
  ts/unknown-kind/unknown-ns reject; strict и shadow-режимы), читатели
  `supersede_impact` / `projection` (Kahn, стабильный порядок) /
  `causal_freshness` (длина цепи, не позиция курсора), `migrate` (идемпотентный
  добор reply/branch/supersedes), `check_invariants` (dangling/ts/циклы DFS).
- Живые писатели: transport reply-mirror; materialize_todo → causes;
  revise_contract → supersedes; topic_memory add_item/confirm → supersedes;
  create_branch_row → causes; set_branch_status(joined) → событие+merge.
- Миграция НЕ удаляет исходные колонки (денормализация, п.6 stage003).

## Приёмка (фактические прогоны)

1. Копия прод-state.db (363MB, 38k messages, 13.6k observations): миграция 0.2s,
   4502 ребра (reply 4474, causes 28, merge 13), skip=0, replay: new=0
   absorbed=4502, инварианты зелёные. reply_unresolved=856 — родители вне поля
   наблюдения (докорректная граница охвата, не ошибка).
2. Стек контроллера: 199 passed (177 прежних без правок читателей + 22 новых).
3. Новые регрессы (24): форма DAG fork→merge, инвалидация от вершины,
   reject цикла писателем, crash seam (событие без рёбер добирается),
   SIGKILL посреди миграции ×3 → инварианты зелёные, итог = бесперебойному прогону.
4. Бенчмарк на прод-объёме: freshness 0.03ms, impact 0.23ms, projection 0.01ms
   (гейт 50ms).
5. Два бага, существовавшие до этой работы, в branch-сетке
   (test_session_mutation_branch*, home_io_guard) воспроизводятся на чистом HEAD —
   не наши.

## Не сделано / ограничения

- `about`-писатель не подключён (нет поставщика «тот же предмет» до
  topic_detector-гейта); схема его допускает.
- Читатели не переключены — shadow-фаза; linear projection готова как drop-in.
- Прод-миграция на живом state.db ещё НЕ выполнена (только копия): запускать
  через ctl_задачу по директиве Вл, тогда же включить reply-mirror на живом
  трафике.
- origin_server_ts в event_observations не хранится — с ним reply-исключение
  можно будет сузить до честного ts событий (отдельная правка транспорта).

## Ревизия v2 (вечер 03.11, после resend ТЗ)

ТЗ пришёл повторно с тремя поправками. Сверка: (1) уникальный индекс + отказ от
AUTOINCREMENT — НЕ исполнено (DDL нёс AUTOINCREMENT и индекс `udl_ctl_causal_link`);
(2) `<table>:<id>` как единое пространство — исполнено по смыслу (namespaced-концы
ctl:/task:/rev:/mem:/todo:/br:/sess:/obs: — алиасы доменов, форма `<scope>:<id>`
соблюдена); (3) единый writer reply-зеркал в точке ingesta — исполнено (transport).

Исправлено: DDL приведён к v2 (rowid-alias без AUTOINCREMENT, индекс `ux_cl_edge`)
в SCHEMA_SQL и causal.py; `ensure_schema` добавляет `ux_cl_edge` и убирает legacy
`udl_ctl_causal_link` (создаём новый раньше, чем роняем старый — ключ идемпотентности
не оголяется). Найден прод-след: живой state.db уже содержит таблицу СТАРОЙ формы
(создана startup-схемой гейтвея, 0 строк — теневая фаза не нарушена). После
первого вызова causal.ensure_schema она примет v2-форму; перестроение таблицы с
AUTOINCREMENT не делалось (пустая, остаток косметический — резолвится при прод-миграции
по директиве). Новый регресс: legacy-v1 БД → ensure_schema → v2-форма, строки и
уникальность сохранены. Прогоны после правок: causal 25/25, стек контроллера см.
ниже, прод-копия: миграция 0.37s, 4536 рёбер, replay new=0, инварианты GREEN,
freshness 0.107ms.

## 04.11: директива «возвращай обратно» — платформо-слепой граф (bb93981dd0)

Вл (04.11): механизмы не знают платформ; недавняя кампания чистки матрикс-привязок.
Аудит stage a–d: узлы графа всегда были внутренними (ctl/task/rev/mem/todo/br/sess/obs),
никаких `mx:` в рёбрах; единственная привязка — causal.py сам парсил литерал
`m.in_reply_to` из event_observations.relation в двух местах. Исправлено: экстрактор
`relation_parent_event()` переехал в transport.py (граница транспортных фактов, рядом
с m.room.encrypted special-case), `mirror_reply_for_event` принимает готовый
`parent_event_id`, миграцияreply больше не грепает платформенный ключ в SQL.
causal.py не содержит ни одного платформенного литерала. causal 25/25, ruff clean,
lefthook green. Тень не нарушена: ctl_causal_link в живом state.db по-прежнему 0 строк;
читатели включаются по одному после ратификации.

## 04.11: stage-a исполнен на проде (мандат «го stage-a»)

Бэкап: /data/state.db.stage-a-backup (377MiB, SQLite backup API поверх живой БД).
Миграция causal.migrate() на живом state.db: 4578 рёбер (reply 4537, causes 28,
merge 13), reply_unresolved=856 (родители вне окна наблюдения — норма для
out-of-order sync, сметает последующий migrate). Повторный прогон: absorbed-only
(new=0) — идемпотентность подтверждена на прод-данных. Остаток AUTOINCREMENT из
legacy-формы устранён атомарной пересборкой таблицы за 8ms (строки и распределение
kind идентичны до/после), DDL приведён к ТЗ v2 (INTEGER PRIMARY KEY rowid-alias).
Индексы: ux_cl_edge + idx_{kind,to,from}, udl_ctl_causal_link сброшен.
Инварианты GREEN. Шadow-writers включатся в процессах после их рестартов (гейтвей
живёт с 03.11 12:55 — старше causal-коммитов); читатели по-прежнему выключены —
stage-b по ратификации.


## 04.11: stage-b — миграция обратной силы + инварианты (мандат «го stage-b»)

Донор-аудит §2.4 п.4 реализован как `causal.mirror_coverage()`: заново выводит
ожидаемое множество reply-рёбер из всего корпуса `event_observations.relation`
через тот же transport-экстрактор и вычитает то, что лежит в таблице. Отвечает
на вопрос, который `migrate` о себе ответить не может: `reply_unresolved` —
счётчик только текущего прогона, а аудит меряет покрытие целиком. platform-blind
(литералов отношений нет), реестра не пишет — это мера, фикс = идемпотентный sweep.

Прод-цикл (живой state.db, WAL, busy_timeout 30s):
1. Бэкап перед записью: /data/state.db.stage-b-backup (backup API, 4578 рёбер в снапшоте).
2. `migrate` добрал дельта трафика после stage-a: +12 reply (4537→4549), skipped=0.
3. Покрытие: expected=5405, resolved=4549, **missing=0**, unresolvable=856 —
   ровно хвост родителей вне окна наблюдения, инвариант п.4 выполняется.
4. `check_invariants` = GREEN; replay: new=0 (absorbed-only) — идемпотентность
   подтверждена повторно на прод-данных.
5. Бенч: coverage 22.8ms (полный пересчёт 13.9k наблюдений, гейт 50ms),
   freshness 0.007ms.
6. Регрессы: +6 тестов покрытия (green-after-migrate, флаг молча удалённого
   ребра, unresolvable-хвост, отсутствие фантомов на нереply-отношениях,
   платформо-слепость, полный цикл stage-a→трафик→аудит→sweep). Стек
   контроллера: 200 passed (194 прежних без правок + 6 новых).

Регресс теста цикла: `record_observation` зеркалит reply ЖИВЫМ писателем,
поэтому дельта «missing» появляется только при out-of-order (ребёнок раньше
родителя) — так и задумано; первый вариант теста это учитывал неверно.

Тень не нарушена: читатели по-прежнему выключены. Писатели живого трафика
(transport reply-mirror) включатся с рестартами процессов гейтвея; до этого
разрыв закрывает periodic `migrate` — missing=0 при выключенном write-path
подтверждает, что схемы достаточно.

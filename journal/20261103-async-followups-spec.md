# Мини-спека: асинхронные следствия на тике 1 Гц (слаг: асинк-следствия; stage003)

Статус: НА РЕВЬЮ ВЛ — кода нет до аппрува. Автор формулировок — агент, каноном не является.

## Основание (дословно)

Вл 02.11: «п.3 асинхронные следствия на тике 1 Гц … напоминалка = запись со сроком +
счётчик тактов; обзор = периодическое событие темы; readUntil/idleAt из донора как
референс». База — тик 1 Гц (`32cf3d9a08`, doctrine 02.11: interval'ы = счётчики тактов,
idle-такт ноль SQL). Правило профиля: «отложенное — событие, не таймер».

## Принцип

Следствие = строка в БД, а не объект в памяти процесса. Тикер просыпается,
находит дозревшие записи, исполняет следствие через существующий порт
(`controller/outbox.py` → Home-канал; прецедент — уведомление о failed-RUN).
Перезапуск шлюза ничего не теряет: состояние всегда в state.db.

## Таблица `ctl_followup` (канон SCHEMA_SQL, writer = controller package)

```
followup_id    TEXT PK
kind           TEXT CHECK IN ('reminder','review')
scope_task     TEXT          -- ctl_task.task_id (наполняемый контекст, не NULL)
scope_project  TEXT          -- надтема, nullable
text           TEXT NOT NULL -- что сказать человеком
due_at         REAL          -- reminder: wall-clock срок (человекочитаемый)
period_s       INTEGER       -- review: период, only kind='review'
state          TEXT CHECK IN ('pending','awaiting_ack','done','cancelled')
last_fired_at  REAL NULL
beats_since_0  INTEGER DEFAULT 0   -- счётчик тактов перепросрочки
retries        INTEGER DEFAULT 0   -- попыток доставки последнего fire
created_by     TEXT NOT NULL       -- 'user-directive'|'agent-proposal'
created_at     REAL NOT NULL
provenance_json TEXT               -- msg-id ленты / RUN id — по stage003
```

Индексы: `(state, due_at)` / `(state, last_fired_at)`; scan дешёвый.

## Напоминалка (kind=reminder)

Тикер (каждый такт, один запрос с индексом):
1. `pending AND due_at <= now` → fire: текст уходит через ctl_outbox на Home-канал
   («Напоминание: <text>», followup_id и provenance — в payload). Запись →
   `awaiting_ack`, `retries=0`.
2. `awaiting_ack`: повтор через `repeat_beats` тактов последней попытки
   (`retries += 1`), лимит `max_repeats`; после исчерпания — alert «не дозвонилось»
   и `state=done`. Референс донора: evals REQUEUES → unavailable. Сама семантика
   ack (что считается «принял») — решение Вл, см. вопросы.

Атомарность fire = существующая обвязка `_execute_write` (BEGIN IMMEDIATE),
idempotency-key = followup_id + номер попытки: двойной тик не задублирует отправку.

## Обзор (kind=review)

Тикер: `pending AND last_fired_at + period_s <= now` → событие в тему:
обзор НЕ бомбардирует общий чат, а рождает периодическое событие темы
(topic_id = ctl_task.task_id, stage003); для человека — краткая строка в
Home-канале со ссылкой на тему. После генерации `last_fired_at = now`.

## Границы

- Агент создаёт followup ТОЛЬКО в scope текущей задачи (правило stage003:
  отсутствие указанного scope = локально, never global). Human-directive — любой scope.
- Никаких новых потоков/таймеров; один вентиль в `Ticker._schedule`
  (счётчик `followup_scan_every=1` такт — см. вопрос).
- Tool-поверхность (v2, вне этой спеки): сейчас создание — только через
  controller API/CLI агента, чтобы не плодить параллельных механизмов записи.

## Вопросы, где решение за Вл (по одному, начну с первого)

**ack-семантика** (ранее «Q1»). Признавать ли «дозвонилось» только
явной реакцией/словом человека (надёжно, но надо ловить inbound), либо упрощённо:
repeat_beats × max_repeats без ack — после исчерпания просто alert+done?
(Донорский референс — упрощённый вариант: requeue limit → unavailable.)

# 2026-11-09 — controller: park-and-continue, настоящие бюджеты, wake seam

Го Вл «го на все пункты» по разбору критика (1-Гц доктрина vs реальность).

## Что сделано
A. loop.py: `return` после transition(needs_human)/transition(blocked) → `continue`.
   Приход в needs_human/blocked выбрасывает задачу из Q.eligible, следующая
   ready поднимается в этом же drain, а не через ≤300 с. (Пункт 2 разбора.)
B. Бюджеты стали настоящими: раньше `limit_value` в ctl_budget_counter не писал
   НИ ОДИН код-путь — `budget_exhausted` был тождественно False. Добавлено:
   `model.set_budget` (upsert лимита, used не трогает),
   `model.apply_contract_budgets` (контрактовский budgets применяется на ready→running
   в transitions), CLI `controller.pump_cli budget set|show` (пункт «ни один CLI-путь»).
C. Wake seam: `controller/wake.py` — файл-колокольчик (~/.hermes/controller.wake,
   env HERMES_CONTROLLER_WAKE для тестов). transitions (любое committed
   изменение) и followups.add звонят; ticker._schedule на каждом бите сверяет seq
   и затягивает next_pump/next_followups в текущий бит. Idle-бит остаётся zero-SQL
   (файл-ридь, не БД — 28.10-класс клинча исключён). (Пункт 1: «latency — политика
   due, а не такта».)
D. docs/design/maintenance-epoch.md — проект самосогласия maintenance-класса
   (per-scope грант + бюджет). 4 открытых вопроса к Вл, без них не включается.

## Проверка
- tests/plugins/test_controller_wake_budget.py — 7 новых (set_budget оживляет
  страж; контрактовские бюджеты легли на running; transition/followup звонят;
  wake тянет pump в текущий бит при интервале 3600).
- tests/plugins/test_controller_loop.py — 2 новых регрессии на starve-баг.
- Итог: wake_budget+loop+ticker+pump_cli+step_pause = 58 passed.
- Порты не затронуты (wake.py — новый модуль; правки наших controller/* по го).

## Не сделано / ограничения
- Топологический переворот («kernel loop = главный процесс, гейтвей — гость») —
  не делал: меняет базис NO-GO аудита, ждёт отдельного вердикта.
- hermes_events (30с wait) с тикером не сшит — следующий шаг после приёмки этого.
- Ветка не смержена в main; рестарт не просил. После рестарта живёт на prod.

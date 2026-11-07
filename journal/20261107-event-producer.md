# Event-producer v0 + миграция убитых джобов

Ветка `elk/event-producer-v0` (07.11). Статус: к ратификации, в main не мержено.

## Лег 1: внешний продюсер событий

`scripts/task_emit.py <event_id>` — CLI без агентского хода: открывает state.db,
будит подписчиков `wait_event=<event_id>`, доставка детерминированная
(`hermes send`). systemd-timer становится ИСТОЧНИКОМ события, а не ходом агента
— это и есть «отложенное — событие, не таймер».

Цикл замкнут тестом: тикер → emit → задача из wait_event пере-доставляется →
ack владельца закрывает её → стартует следующая ready.

**Попутный фикс (важный):** ack владельца (`/next`, реплай) больше НЕ съедает
голову, припаркованную на событие (`wait_event IS NULL` гвард в
`on_owner_message`/`skip`). Курсор блокируется, но не теряется; готовая задача
за припаркованной головой ждёт пробуждения. Тест `test_ack_does_not_consume_event_parked_head`.

## Лег 2: разметка 22 убитых джобов (jobs.json.bak-kill-all-0611)

### A. no_agent script-джобы → systemd user timers 1:1 (4)
| джоб | расписание | решение |
|---|---|---|
| daily-trace-harvest | 23:30 daily | unit OnCalendar=*-*-* 23:30 |
| hermes-mcp-listing-watchdog | каждые 6ч | OnCalendar=00/6:00:00 |
| idle-intake-interesting | 00:30 daily | unit 00:30 |
| weekly-trace-digest | Пн 09:00 | unit Mon 09:00 |

Скрипты уже существуют; юниты вызывают их напрямую. Агент не участвует.

### B. prompt-джобы (агентский ход по расписанию) → timer→event→task (5)
| джоб | было | кандидат |
|---|---|---|
| secretariat-book-scan-watch | daily 09:00 | task с wait_event=secretariat-scan; таймер эмитит ежедневно |
| upstream-watch | daily 09:00 | wait_event=upstream-check (дельту считать от main!) |
| self-improvement-watch | Пн 10:00 | wait_event=selfimprove-tick |
| win-runner-recovery-watch | каждые 5м (!поллинг агентом) | .path/.socket активация или монитор-скрипт-продюсер, агент только по факту события |
| donor-reports-milestone | каждые 15м (!) | пересмотреть: поллинг 15м агентским ходом = cron-мышление; типовой кандидат на выкидывание или event |

Для B нужно периодическое возрождение задачи после done (сейчас emit будит
только существующих подписчиков). Пробел: PERIODIC-from-completion для tk_task
— отдельный маленький механизм (рекуррент-правило в ядре), предлагаю v0.3.

### C. autonomy-tick — не мигрирует (принцип)
Тик сам себя создал из крон-мышления; его полезные находки (осиротевшие ветки)
переезжают в разовые задачи по необходимости.

### D. disabled (12) — в никуда
france-channel-watch, auth-access-decision-reminder, gl-14day-watch,
win-uac-click-watchdog, controller-pump-tick (заменён pump-lock), 
agency-discussion-moment, human-gate-watchdog, cron-liveness-watch
(самоreferенциальный), critic-return-memgate-2810, ticker-deploy-verify-2810,
topic-gate-drift-audit (вручную verb'ом), critic-item2-wake.
Не выкапывать без запроса Вл.

## Не вошло / ограничения
- Рекуррентность задач ядра (B) — v0.3, не в этой ветке.
- systemd-юниты НЕ устанавливаю (инфра-действие вне репо) — скелеты приложу
  после ратификации маппинга.

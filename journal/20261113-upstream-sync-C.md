# Upstream sync C — финал (13.11)

Ветка `elk/upstream-sync-20261110` = `ed768d9e3a`, main = там же, origin верифицирован `ls-remote`.

## Состав
- `11fa31bbe3` — merge upstream/main (дельта 1804 коммита на 3fe1962f8b). Единственный
  конфликт — `hermes_cli/config_migrations.py`: upstream `clarify_timeout` = шаг 53,
  наш write-approval retirement сдвинут в 54; `_config_version` → 54.
- `ed768d9e3a` — merge main (cron excision + administrator-mode линия + dream-command)
  обратно в ветку. Конфликты: `cron/scheduler_delivery.py` и
  `tests/cron/test_cron_live_delivery_confirmation.py` — deletion форка сохранён;
  `hermes_cli/main.py::_AGENT_SUBCOMMANDS` — union без `cron` (gateway + upstream curator).

## Гейт (вариант C, Вл 10.11)
Полный изолированный прогон `scripts/run_tests_parallel.py` (каждый файл — отдельный
процесс), 12 воркеров, оба дерева (~55k тестов × 2):
- merged: exit=1, 230 отказных файлов / 418 отказных тестов;
- идентичный набор отказов в base; **merge-only = 2 файла**
  (`test_session_approval_commands.py`, `test_tui_gateway_notification_poller.py`);
- base-only = 1 (`test_config_migration_nested_guards.py`) — **чинится этим мержем**
  (наш resolution v53/v54);
- 8 файлов «no tests ran» — идентичны в обоих деревьях (environment: npm/build/live);
- флейк-замер: 24/24 зелёных в изоляции (оба файла × оба дерева × 3 попытки),
  файлы мержем не тронуты ⇒ load/order-эффект полного прогона. Классификация
  повторена за гейтом 06.11 (order-flake precedent).
- Одинокий pytest-прогон (`-q`) непригоден на этом хосте: кросс-файловый каскад
  умирает INTERNALERROR/exit=75 на 4% одинаково в обоих деревьях. Отчёт ночного
  гейта 12.11 с «0 regressions» был вакуумным (ни одна сводка не отпечатана) —
  урок проустановлен как `wake_check` в tasks_kernel (elk/agency-mechanisms-v0,
  c478a3da61): машинная проверка достаточности данных на каждой доставке/ack.

## Состояние после
- Рабочее дерево основного репо (main) обновлено мержем при живом шлюзе —
  **активация требует рестарта, шаг Вл**.
- `sync-nightly-1110.timer` остановлен и выключен (контракт задачи исполнен).
- worktree `/home/qwen/ha-sync-20261110` удалён; база сравнения — t/5 (можно снести).

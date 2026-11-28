# CHANGELOG-fleet

Хроника правок ветки хранения форка (ТЗ «агент-хранитель форка Hermes», 28.11).
Формат записи: пункт ТЗ (Д*) → коммит/ветка → положительная проверка (вывод теста) → статус выкатки на ноды (rtx5090 → Win → zoo).
Откат каждой правки: один `git revert` или `hermes update` на предыдущий тег — способ указан в записи.

## Реестр инцидентов

| ID | Дефект | Статус |
|---|---|---|
| Д1 | Самоубийство хода при `hermes update` из активного хода (28.11, Win) | 🔴 spike-тесты, реализация в работе |
| Д2 | Зомби-гейт 9¼ ч без обработки Matrix (28.11); примитивы свежести есть, потребителя внутри форка нет | 🔴 spike-тесты, реализация в работе |
| Д3 | Потери трнов «Your request was not processed» (12 случаев на снимке GEPA 28.11) | ⏳ очередь (P0, следующая итерация) |
| Д1b | Win 28.11 13:31: updater SIGKILL'нул гейтвей в окне drain (гейтвей объявил 1800s defer, ACK нёс ~30s config drain) → пользовательский turn ампутирован → sticky `unknown_execution` паузы, отбивающие каждое сообщение | P0 | **реализовано 28.11**: (а) pause-ACK несёт эффективный deferral-budget (max(config, _restart_after_turn_timeout) + active_work); (б) durable receipt активных leases в момент паузы → boot settle как `update_interrupted` (никогда не re-run, consume-once, TTL 3600s); (в) person-unknown при второй встрече оседает terminal (one-shot) | `tests/gateway/test_fleet_maint_d1b_*` 8 passed |
| Д4 | Дубли `platform_message_id` (11 случаев, gap 0 с) — дедуп в памяти адаптера, не переживает рестарт | 🔴 spike-тесты, реализация в работе |
| Д5 | След временного патча `runtime_ownership.py` на rtx5090 — в живом дереве `main@38130e4660d` **не найден** (28.11 read-back); проверить эквивалентность upstream-версии | ⏳ P1 |
| Д6 | E2EE на Windows: python-olm недостижим (manylinux-only). Режим `optional`, предупреждение при старте — штатное | ⚪ наблюдение, кода не требует |
| Д7 | Взаимная заводка ботов служебными формами (28.11, #hermes-win; **ТЗ v2 — P0**): m.text-формы приёма/очереди, 20+ форм/мин read-back из комнаты | 🔴 spike-тесты, реализация в работе |

## 28.11 — spike: контракты Д1/Д2/Д4 заморожены тестами (elk/fleet-maint)

- Ветка: `elk/fleet-maint` от `origin/fleet/upstream-sync@bd5eea7fc96`.
- Правка: только `tests/gateway/test_fleet_maint_d{1,2,4}_*.py` + этот файл + `docs/fleet-maint-recon.md`. Продуктовый код не тронут.
- Проверка: `pytest` — зелёные пины существующих примитивов (маркер `_HERMES_GATEWAY`, atomic heartbeat, in-memory dedup) + строгие `xfail` на отсутствующих механизмах; xfail обязан погаснуть при реализации (strict=True не даст забыть).
- Откат: `git revert` этого коммита (тесты не влияют на рантайм).
- Выкатка: не требуется до появления продуктовой правки.

## 28.11 — spike Д7 (ТЗ v2): контракт подавления form-storm (elk/fleet-maint)

- Правка: `tests/gateway/test_fleet_maint_d7_service_form_storm.py` + обновление реестра (Д7 из кандидатов → P0).
- Разведка: приёмная конвенция есть — чужой `m.notice` отбрасывается (default `process_notices=false`); шторм живёт на **отправке**: все служебные формы идут `m.text` (read-back комнаты: 20+ форм за 70 с, оба бота).
- Проверка: `pytest -q` — `4 passed, 7 xfailed` (зелёный пин конвенции notice-drop; строгие xfail: notice-отправка форм, фильтр «не адресовано — не в очередь» в `admit_message`, штатный silence-marker для bot-vs-bot).
- Откат: `git revert` коммита. Выкатка: как выше.

## 28.11 — P0 итерация 1: Д1 hand-off + Д2 stale-transport guard (elk/fleet-maint)

- **Д1**: `hermes_cli/update_cmd.py::gateway_turn_handoff_argv` (решение по маркеру
  `_HERMES_GATEWAY`) + вызов из `main.cmd_update` до hangup-защиты/лока; детей —
  detached setsid/`windows_detach_popen_kwargs`, `--gateway` режим (prompt → default по
  таймауту), лог `$HERMES_HOME/update-gateway-handoff.log`. xfails Д1 погашены.
- **Д2**: `gateway/run_watchers.py::GatewaySessionWatchersMixin._stale_transport_guard` —
  внутренняя (не крон!) реакция на живой-луп/мёртвый-транспорт: возраст `_last_sync_ts`
  подключённого matrix-адаптера > 600 с два тика подряд → critical diag +
  `request_restart(via_service=True)` (drain-aware, durably admitted очереди живут в
  state-store). Регистрация в `_POST_RECONNECT_WATCHERS`. Оптовут `stale_transport_watchdog=false`.
  Поведенческие тесты (зомби-инъекция + негатив) вместо xfail.
- Проверка: fleet-maint suite `9 passed, 4 xfailed` (остались Д4/Д7 — следующая итерация);
  consumers `test_cmd_update.py`, `test_old_updater_main_shims.py`, `test_local_runtime_updates.py`
  — `6 passed, 3 skipped`; imports чисто.
- Известно: `test_cmd_update_docker.py::...guidance...` падает и на чистой базе bd5eea7f
  в комбинации main-venv × worktree (ретаргет владеющей установки) — не регрессия
  (базовая проверка на detached worktree).
- Откат: `git revert` этого коммита. Выкатка: после мержа — ноды `hermes update`'ом.

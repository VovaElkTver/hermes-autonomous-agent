# 15.11 — R4 активация: пост-чеклист + boot-sweep armed-approvals (П4)

## Пост-чеклист рестарта 16:10 (всё зелёное, живые замеры)
- Restart: MainPID 34086, старт 16:10:39 МСК; matrix connected 16:10:48 (main + painter).
- L1 heartbeat трёхтокенный: `34086 <ts> {"matrix":1.96,"webhook":"unknown"}` ✓
- `sync_token.json`: `~/.hermes/platforms/matrix/store/` И `profiles/painter/.../store/` ✓ (путь в чеклисте был неточен — store/ под каталогом адаптера)
- Статус: без degraded.
- L2: watchdog-юнит = канон форка (md5 совпал с `scripts/gateway-watchdog.py`), dry-run читает pulses ✓.
- Шаг-2 активирован (Вл «делай» п.1-3): drop-in `hermes-gateway-watchdog.service.d/hang-action-active.conf` → `WATCHDOG_HANG_ACTION=1` (hang + DEAF-STALL рестарты; бюджет 2/30мин и freeze-страховки действуют).

## П4 — boot-sweep armed-approvals (Вл: «делай» = отдельная ветка)
- Ветка `elk/armed-approval-boot-sweep`, коммит `cf51e88d80`, FF в main, запушен.
- `tools/approval_ledger.py`: `sweep_restart_orphans(boot_ts)` — rows pending/expired без resolution, созданные до бута, группируются в notice по чату (`_chat_target` парсит session_key: matrix room/dm с сервером, telegram numeric, unroutable → только лог). `mark_restart_lost` — flip в `lost_notice`/`restart-lost` **только после успешной доставки** → недоставленное повторится на следующем буте.
- `gateway/run_bootstrap.py`: таск `_sweep_lost_approvals` после READY (+5s на первый sync), доставка через `controller.alerts.deliver` (`hermes send`), fail-open.
- Тесты: 11 новых + существующие ledger = 23 passed на mergesult; ruff чист.
- Флаг гейта: общий с `approvals.durable_pending_ledger` (= true прод). Живых pending-сирот в state.db нет (27 expired — до introducing дедуп-полиси;notice на первом буте придут по старым expired-строкам! см. риск).
- Активация: рестарт через transient unit `deferred-gw-restart-sweep` (+180с), лог `scratch/deferred-gw-restart-sweep.log`.

## Риски/остатки
- При первом буте со sweep'ом уйдёт notice по 27历史 expired-строкам (созданы до 16:10, resolution NULL) — ожидаемое одноразовое явление, дальше дедуп по state.
- Checkout прода в момент работы слетел на `elk/need2write-apply` (чужой процесс); возврат на main выполнен; мерж делался отдельным worktree, чужие коммиты (8d87075718, 17 правил) не тронуты и НЕ в main — на ратификацию Вл отдельно.
- `elk/deaf-sensor-l1` + `elk/armed-approval-boot-sweep` — уборочное закрытие веток (удал. remote-ref consent-gated).
- Hang-детектор step-2 теперь активен: первый же рестарт watchdog'ом появится в логе `gateway-watchdog.log` с `action=` вместо SHADOW.

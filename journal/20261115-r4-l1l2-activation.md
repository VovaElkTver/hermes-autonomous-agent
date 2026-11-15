# 15.11 R4 контур: активация L1-сенсора + L2-канона watchdog («делай 1-3» Вл)

## Сделано
- main `70654ebbbe` (на origin): L1-сенсор (`gateway/run_watchers.py` — третий токен
  heartbeat, JSON `platform -> age_seconds`; `plugins/platforms/matrix/adapter.py` —
  `receive_evidence_age()` на `_last_sync_ts`) + L2-канон `scripts/gateway-watchdog.py`
  (детектор 3 DEAF-STALL: 180с ×3 сэмпла, лестница freeze->budget->act, SHADOW по
  дефолту) + доставка `hermes_cli/post_update.py` шаг `sync_gateway_watchdog`
  (HOME_STEPS, идемпотентно, fail-open). Доконтур: `docs/design/gateway-deafness-contour.md`
  (решения 1-2 исполнены; 3 SHADOW до статистики/слова; 4 открыт).
  Тесты: 11 зелёных (4 L1 + 3 доставка + TestMatrixDiagnostics incl. receive_evidence_age),
  ruff чистый. 9 предсуществующих падений test_matrix.py (markdown/threads/upload) есть и
  на чистом main — не от этой работы.
- Узел: `~/.hermes/scripts/gateway-watchdog.py` заменён каноном (бэкап
  `.bak-r4l1-20261115`); live dry-run: `active=active hb_age~2s pulses=None -> no action`
  (старый 2-токенный heartbeat до рестарта читается как UNKNOWN — по дизайну).

## Рестарт гейтвея (активация R1+R2+R3+П8+П9+planning-multistage+L1/L2)
Запланирован `systemd-run --user` transient-юнитом (вне cgroup гейтвея): fire ~16:10:06 МСК.
Лог-файл запуска: `~/.hermes/cache/scratch/deferred-gw-restart.log`.

## Проверки после рестарта (следующий сет/тикер)
1. `systemctl --user is-active hermes-gateway` = active, новое время запуска.
2. Через ~мин: `cat ~/.hermes/gateway-heartbeat` — третий токен JSON-карты;
   `matrix` age < 180s (приём жив).
3. `python3 ~/.hermes/scripts/gateway-watchdog.py --dry-run` — в строке `pulses=` карта
   (не None).
4. `journalctl --user -u hermes-gateway -n 50`: preflight R2 / sync-token R3 /
   P8 rehydrate без ошибок.
5. Ответ в Matrix-комнате = proof-of-life приёмного пути.

## Открыто
- Hang step-2 (решение 3): SHADOW; статистика — 1 эпизод 14.11 22:45-22:50 МСК.
- Boot-sweep armed-approvals (решение 4 / остаток П9): последствия вариантов
  разложены Вл 15.11 (отдельная ветка сейчас vs часть AGENCY-активации).
- Незакрываемое без согласия/тикера: ветка `elk/deaf-sensor-l1` (локальная + удалённый
  ref + worktree /home/qwen/ha-deaf-l1) — закрытие блокировано гвард-таймаутом 15.11;
  контент уже в main, закрытие чисто уборочное. Этот журнал-файл не закоммичен
  (remotepush-часть того же заблокированного вызова) — закоммитить тику.

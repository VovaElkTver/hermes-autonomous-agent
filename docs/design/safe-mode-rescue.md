# Safe Mode & Rescue Core — часть проекта hermes-autonomous

Статус: v0 ядра `hermes_rescue` в репо (коммит за `34e5160a3c`), dry-run проверен на живом узле 26.10.
Решение Вл (26.10): не копия основного, не урезанный full-конфиг, а **спец-ядро** — маленькое, задачное, со своими руками.

## Инварианты
1. **Отсутствие по построению.** Rescue не имеет кронов, pump, MCP, плагинов, скиллов, subagents — не флагом, а кодом: ~350 строк, stdlib+httpx, запрет импорта `gateway.*`/`agent.*`. Домен отказа main-машинерии к нему не переходит.
2. **Разум заёмный, руки свои.** Одно ограниченный LLM-вызов (verdict из фиксированного списка). Модель недоступна → детерминированный дефолт `escalate`, руки убираем.
3. **Руки = kit, не shell.** `restart_unit`, `snapshot_db` (online-backup, не cp), `switch_release` (атомарная перелинковка `~/hermes-live` + рестарт), `lift_estop`, `observe(120s)`, `escalate`. Каждая — в журнал `~/.hermes-rescue/journal.jsonl`.
4. **Данные main — только копиями.** Свой HOME `~/.hermes-rescue`, свой журнал; живой state.db открывается read-only + snapshot до любого действия.
5. **Личность одна.** Тот же Matrix-токен/аккаунт: rescue поднимается, когда main мёртв (триггерная модель) — токены не множим. Резидентность (второй токен) — только если подтвердится потребность в MTTR < минуты.
6. **Git не в аварийном контуре.** Сборка релизов — мирная процедура; откат = `switch_release` по указателю, `readlink` как констатация, ноль git-команд при аварии.

## Роли
- `hermes_autonomy` (main): разум, широкие права, право на ошибку. `/reset2safe` — кнопка отсюда в safe-состояние, пока ядро живо.
- `hermes_rescue`: иммунный контур, когда ядро НЕ живо: crash-loop детект (systemd-триггер, v1.5) → inspect → decide → kit → observe → рапорт владельцу → отдать управление main.

## Критерий разумности (стартовый)
- healthy (active, heartbeat <90s, quick_check ok, нет FATAL) → `none`;
- crash-loop → сначала `snapshot_db`;
- `switch_release` только если есть целевой релиз И сигнатура кодовая (import/startup), не config/host;
- всё неоднозначное, db нечитаема, цели нет → `escalate`.
- после действия `observe` 120 с; не ожило → второй попытки нет, `escalate`.

## Дорожная карта
- **v1 (готово)**: пакет + dry-run/`run-once --report`, hands kit, журнал.
- **v1.5**: systemd-пара `hermes-rescue.service` (+ OnFailure/триггер от watchdog-детекта crash-loop) в поставке форка; release-модель (`~/hermes-releases` + `~/hermes-live`) как стандарт развёртывания — тогда `switch_release` боевой.
- **v2**: резидентный режим по решению Вл; ротация LKG = переразвёртывание rescue-кода тем же deploy.

Связано: SPEC-v1.2 (watchdog/heartbeat — переходят в supervisor-поставку форка), `/reset2safe` (`34e5160a3c`).

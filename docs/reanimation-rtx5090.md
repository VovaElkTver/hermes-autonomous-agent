# REANIMATION: Rtx5090 — инструкция для @rtx5000 (санитар)

Ты дежурный реаниматор основной ноды. Читай это ДО любых действий.

## Что реанимируешь
- Юнит: `systemctl --user hermes-gateway.service` (аккаунт Matrix @Rtx5090)
- Код: `~/hermes-agent` (checkout, НЕ installs/), HERMES_HOME `~/.hermes`
- Фактический HEAD и пин — см. `docs/RELEASES.md` (раздел LIVE)
- Цикл: `~/.hermes/scripts/gw_cycle.sh` через `systemd-run --user --collect --on-active=5`

## Диагностика (строго по порядку)
1. `systemctl --user status hermes-gateway.service` — MainPID, NRestarts.
2. `journalctl --user -u hermes-gateway --since "-30m" | grep -E "Error|error|Traceback|no such column" | tail -20`
3. `git -C ~/hermes-agent rev-parse HEAD; git -C ~/hermes-agent status --porcelain; git -C ~/hermes-agent reflog --date=iso | head -5`
4. Сверь HEAD с `docs/RELEASES.md`. Не совпадает — победил чужой пин, верни HEAD из RELEASES, не выдумывай.

## Известные трупы
- `RuntimeError: state.db did not open: no such column: ...` → сборка требует схему новее живой state.db. Лечится ОТКАТОМ checkout на пин из RELEASES.md, не патчем БД.
- «threads can only be started once» в бут-лупе → смотри `docs/RELEASES.md` LIVE: исторически
  затычка = `pass` вместо `thread.start()` в `gateway/runtime_ownership.py` (`start_writer`).
  На 27.10 17:28 затычка с дерева снята, следующий рестарт по чистому HEAD не проверен.
  Если бут-луп вернулся на чистом HEAD — восстанови затычку ровно как в history диффе,
  не изобретай идемпотентность на живой ноде и зови @vovak.

## Запреты санитара
- НЕ съезжать версией ноды «на свою» без слова @vovak. WSL-инцидент 27.10: пин уехал на 17e581e без согласования, нода умерла в crash-loop.
- НЕ `git reset --hard`, НЕ `hermes update`, НЕ патчить gateway/agent на глаз.
- НЕ больше 2 попыток подъёма — дальше стоп и отчёт @vovak.

## Поднял — проверь и доложи
1. MainPID жив > 2 мин, NRestarts не растёт.
2. `git rev-parse HEAD` = пину из RELEASES.md, `git status --porcelain` пустой (кроме отмеченных interim-патчей).
3. Пинь @Rtx5090 сообщением «wake» в DM Home — я сам проверю состояние и доложу.
4. Отчёт @vovak: что было, что сделано, HEAD.

Обновлять этот файл — мне (Rtx5090) при каждой смене версии. Если файл и дерево расходятся — верь дереву и скажи @vovak.

# dsh-B: активация флагов и план реанимации

Дата: 2026-11-16. Merge `85bbf2b6315` (dsh-B) в main. Всё ниже проверено живым
исполнением; команды можно копировать как есть.

## 0. Что мы включаем

| Флаг | Что меняет при ON | Дефолт |
|---|---|---|
| `tools.call_contract_v2` | Вокруг каждого тул-вызова строится замороженная идентичность (`ToolCallSpec`: call_id, args_hash), перед handler'ом работает монотонный guard (может только понижать решение, не повышать); итог + имя понизившего слоя пишутся в `event_observations` и `meta.call_contract` результата | OFF |
| `tools.call_spec_debug` | Мутация args обёрткой = RuntimeError вместо warn-лога. Включать только на отладку! | OFF |
| `approvals.once_and_ttl` | Гранты approve «ровно раз», у pending-запросов TTL (`approvals.pending_ttl_s`, дефолт 3600), три кода отказа в `meta.error.code`: APPROVAL_REJECTED / CANCELLED / UNAVAILABLE | OFF |
| B4 RRULE | Колонка `rrule` в `ctl_followup` — включается не флагом, а использованием: создаёшь followup с `RRULE FREQ=...` — работает парсер; без rrule поведение 1-в-1 старое | всегда доступно |
| B5 | Только CI-шаг `scripts/ci/check_eval_signals.py` + доки. Рантайм не трогает | n/a |

Ключевое свойство дизайна: **при OFF оба контракта кода не выполняются вовсе**
(короткий `.get(..., False)` на чтении конфига). Поэтому главный инструмент
реанимации — не git, а конфиг.

---

## 1. Порядок безопасной активации

1. **Активируем по одному пункту за раз**: сначала `tools.call_contract_v2`,
   через сутки наблюдения — `approvals.once_and_ttl`. Не оба сразу.
2. Правим `~/.hermes/config.yaml`:
   ```yaml
   tools:
     call_contract_v2: true
   ```
3. Рестарт гейтвея — только штатным циклом (см. §3). После рестарта проверить:
   - `systemctl --user show hermes-gateway.service -p MainPID -p NRestarts`
   - хвост журнала: нет traceback'ов, есть обычная строка matrix connected.
4. Наблюдение минимум до конца дня: зелёные тул-вызовы, тронутые approvals,
   отсутствие новых строк `event_observations` с странными decision.

**Проверка, что контракт реально работает** (после ON): в любом tool-результате
должен появиться блок `meta.call_contract` с `call_id`/`args_hash`.

---

## 2. Симптом → действие

| Симптом | Первое действие |
|---|---|
| Странные отказы тула «denied by guard …» там, где раньше было allow | Убедиться, что это guard (в meta/observation виден слой); если блокирует работу — флаг OFF → рестарт (§3) и завести дело |
| Ломается цепочка approval'ов: `/approve` сразу отвечает «cancelled» | Похоже на TTL-резолв: проверить `approvals.pending_ttl_s`; выставить больше или убрать ключ; флаг OFF — вернётся старая логика вечного pending |
| Detached-процесс стал падать с `APPROVAL_UNAVAILABLE` | Это новый код, он честный: раньше висло навсегда. Если мешает — флаг OFF |
| Traceback вида `RuntimeError ... args mutated ...` | Это debug-флаг `tools.call_spec_debug`. Выключить его (warn-лог безопасен) |
| Контроллер перестал реплатить повторяющиеся followup'ы | Проверить `ctl_followup`: строки с `rrule` ре-плейтятся тикером; `journalctl --user -u hermes-gateway.service \| grep followup`; откат = убрать rrule из строки (NULL) |
| Гейтвей падает в crash-loop после правки config.yaml | Опечатка в YAML почти наверняка: `python3 -c "import yaml,sys; yaml.safe_load(open('/home/qwen/.hermes/config.yaml'))"`. Синтаксис ок и падение продолжается → см. §4 |

Журнал смотреть так: `journalctl --user -u hermes-gateway.service --since "10 min ago"`.

---

## 3. Как правильно рестартовать гейтвей

Важно: голый `systemctl --user restart` изнутри процессов гейтвея leads к
orphan-deafness (постмортэм 15.11) и watchdog-слепоте. Штатный путь:

```bash
~/hermes-autonomous-agent/scripts/gw_cycle_planning.sh
```

Скрипт сам себя перезапускает через транзиент `systemd-run --user` (свой
cgroup), делает паузы и проверяет здоровье. Есть сухой прогон:

```bash
GW_CYCLE_DRYRUN=1 ~/hermes-autonomous-agent/scripts/gw_cycle_planning.sh
```

Правила:
- **Не рестартить серию подряд без паузы.** Между двумя рестартами ≥ drain-пауза
  (минуты, не секунды): каждый рестарт рвёт текущий ход.
- Перед kill любого процесса смотреть `/proc/<PID>/cgroup`, не cmdline —
  у observer/dev/main cmdline идентичен.

## 4. Аварийный откат кодовой базы (если crash-loop не лечится конфигом)

Штатный уровень отката — флаги OFF; для этого git не нужен. Git-уровень — на
случай, когда чинить нечего и надо вернуть вчера:

```bash
cd ~/hermes-autonomous-agent
git status                      # убедиться, что рабочее дерево чисто (иначе сохранить изменения!)
git fetch origin
git reset --hard 5337c3df13d    # коммит прямо ДО merge dsh-B (known-good)
~/hermes-autonomous-agent/scripts/gw_cycle_planning.sh
```

Откат вперёд после расследования:
```bash
cd ~/hermes-autonomous-agent && git fetch origin && git reset --hard origin/main
```

Внимание: если прод-бинарь берётся из другого чек-аута (проверка:
`readlink -f /proc/<MainPID гейтвея>/cwd` и `readlink -f $(which hermes)`),
откатывать надо именно тот каталог, который реально исполняется.

Ничего разрушительного с state.db делать не нужно:
- новые колонки `event_observations` (call_id, args_hash…) и
  `ctl_followup.rrule` — nullable/additive; старый код их просто не читает;
- дропать колонки руками запрещено: это двухшаговые миграции, полудроп
  ломает реконсиляцию схемы;

Watchdog: таймер каждые 30 с; hang-детектор в SHADOW-режиме. Если после
активации шлюза гейтвей рестартится без твоей команды — смотри
`~/.hermes/logs/gateway-watchdog.log`, но помни: shadow ничего не делает сам.

## 5. Куда смотреть за доказательствами

| Что | Где |
|---|---|
| Идентичность вызовов (B1+B3) | `meta.call_contract` tool-результатов; строки `event_observations` с непустым call_id |
| Коды отказа approval (B2) | `meta.error.code` ∈ {APPROVAL_REJECTED, APPROVAL_CANCELLED, APPROVAL_UNAVAILABLE}; ledger: таблица `pending_approvals` |
| TTL pending | ключ `approvals.pending_ttl_s`; ленивая уборка на чтении леджера |
| RRULE-followups (B4) | таблица `ctl_followup`, колонки `rrule`/`next_due`; `ctl_followup list` |
| CI-гард (B5) | `scripts/ci/check_eval_signals.py` → exit 0 |

Тесты всей ветки (рефERENCE):
```bash
cd ~/hermes-autonomous-agent && .venv/bin/python -m pytest -q \
  tests/tools/test_tool_contract.py tests/tools/test_approval_contract.py \
  tests/controller/test_followup_rrule.py tests/ci/test_eval_signal_guard.py \
  tests/controller/test_controller_followups.py
```

## 6. Известное окружению (не dsh-B)

- Тест `test_native_profile_registry_grant` на этом хосте красный из-за
  stale pm-install (bootstrap >60 c); лечится `hermes pm repair`.
- Полный pytest имеет фон flaky-файлов (pty/update/desktop-posix), на baseline
  то же самое.

— подготовлено агентом @rtx5090, 16.11.2026.

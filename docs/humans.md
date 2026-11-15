# docs/humans.md — для владельца узла (люди)

Краткая операционная документация форка `VovaElkTver/hermes-autonomous-agent`
для человека, который владеет/ведёт узел. Подробности — по ссылкам; это
вход, не полная форма.

## Что это

Форк Hermes Agent, ориентированный на автономную работу: ветки разговора
(fork/join), очередь контроллера (managed autonomy), событийный тик,
ресюй-кит, флот нод. Тезисы проекта и выученные ошибки —
`docs/preface-autonomous-agent.md` и `docs/cautionary-autonomous-agent.md`
(читать до, а не после). Отличие от upstream: upstream — только read-only
(fetch да, push/PR нет).

## Установка

Одна команда (Linux/WSL):

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/VovaElkTver/hermes-autonomous-agent/main/fork-install.sh)
```

Windows (PowerShell):

```powershell
iwr -useb https://raw.githubusercontent.com/VovaElkTver/hermes-autonomous-agent/main/fork-install.ps1 | iex
```

Станет: клон форка в `~/.hermes/hermes-agent` (или `<HERMES_HOME>/hermes-agent`)
+ pm-управляемое окружение (pinned uv, bootstrap Python, venv). Скрипт
`setup-hermes` — единственная точка реальной установки; installer только
разрешает репо и назначение.

Переопределения (env): `HERMES_FORK_URL` (источник клона),
`HERMES_FORK_BRANCH` (по умолчанию `main`), `HERMES_HOME`
(по умолчанию `~/.hermes`), `DRY_RUN=1` (напечатать план, ничего не трогать —
сначала на незнакомом хосте).

После установки, по порядку:
1. Секреты — в `~/.hermes/.env` (только секреты: ключи провайдеров, токены
   платформ). Настройки — в `config.yaml`, не в `.env`.
2. Настройки — `hermes config set ...` (ручной правкой защищённый
   `config.yaml` не править).
3. Гейтвей (только если нужны мессенджер-поверхности): `hermes gateway install`,
   проверка — `hermes gateway status`.
4. Завершение: `source ~/.hermes/hermes-agent/activate && hermes --version`.

Установка в уже существующий каталог чужого происхождения (другой origin)
отказывается: это защита, а не баг. Решение — за владельцем: перенести/
удалить каталог или передать `HERMES_FORK_URL`.

## Обновление

```bash
hermes update
```

Fast-forward чек-аута на `main` (авто-stash/restore локальных
изменений) + хвост обслуживания: миграция конфига с бэкапом/откатом,
синхронизация bundled-скиллов (контент-дифф, сходится при повторных прогонах),
целостность state.db, пере-проvisioning рантаймов через PM.

Что не трогается: `venv`, `.git`, `.env`, `node_modules` и всё, что живёт в
`~/.hermes` вне чек-аута (конфиг, скиллы, state.db).

Формализация «обновлялось ли и что было сделано»:
- `~/.hermes/logs/update.log` — пошаговый журнал последнего обновления
- `~/.hermes/logs/update_receipts/latest.json` — чек-аут итога
- `git -C ~/.hermes/hermes-agent reflog --date=iso` — точные таймстампы
  pull/merge
- `~/.hermes/state-snapshots/<ts>-pre-update` — снапшот состояния до обновления

Откат: целевая SHA возврата записывается в `DEPLOY-NOTE.md` (корень репо)
до операции; сам откат — `switch_release` (атомарная перелинковка
`~/hermes-live`), в аварийном контуре git-команд нет. Подробности —
`docs/design/reanimation-protocol.md` и `docs/design/safe-mode-rescue.md`.

## Рестарт гейтвея

Человеческая команда: `hermes gateway restart`.

Не делать: не убивать процесс вручную и не рестартировать из сессии, где
могут жить planning-форки — они дети гейтвея, и голой рестартой их
ампутирует. Если форки возможны, использовать цикл с дреном:
`~/.hermes/scripts/gw_cycle_planning.sh` (по умолчанию ждёт их до 600 с).
После рестарта убедиться, что `MainPID`/`ExecMainStartTimestamp` реально
сдвинулись — `active (running)` ≠ работает.

## Автономия (managed autonomy)

Включается `controller.enabled: true` в `config.yaml` (по умолчанию выключено —
поведение Hermes не меняется). Команды распознаёт код до агентного хода,
RU/EN свободная формулировка:

| Команда | Пример | Эффект |
|---|---|---|
| enable | «работай автономно» | permission on, очередь идёт в области workdir контракта |
| pause | «пауза», «замри, обсудим» | новые диспетчи блок; идущему — interrupt_requested |
| resume | «продолжай автономно» | permission on; старые approvals умирают |
| cancel | «отмени задачу task-xxxx» | task.state=cancelled |
| agree | «иди task-xxxx» | задача согласована, pump диспатчит при включённой автономии |
| status | «покажи статус задач» | список task / contract_version / permission |

`/for` — принуждение множества через очередь контроллера (детальная
семантика: `docs/controller.md`). Полный реестр слэш-команд —
`website/docs/reference/slash-commands.md`.

## Где живое состояние

- `docs/RELEASES.md` — живой пин узла (что LIVE, откатные ветки, инциденты).
- `docs/design/reanimation-protocol.md` — работа с живым гейтвеем (билет на
  выход, DEPLOY-NOTE, «торжество над трупом»).
- `ToDo.md` (корень репо) — порядок работ по stageNNN.

## Для агентов

Смежная пара — `docs/agents/` (operating.md — дисциплина; node-ops.md —
install/update/restart/rollback). Она доставляется вместе с чек-аутом на
каждой установке и обновлении, а указывают на неё скиллы
`autonomous-ai-agents` (install-fork-agent, controller-protocol),
синхронизируемые в `~/.hermes/skills/` при инстале и апдейте.

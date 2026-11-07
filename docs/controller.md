# Managed-autonomy controller — команды и ограничения

Флаг: `controller.enabled: true` в config.yaml (или `plugins.entries.controller.enabled`,
или env `HERMES_CONTROLLER_ENABLED=1`). Off by default — поведение Hermes не меняется.

## Команды (распознаёт код, до агентного хода; RU/EN свободная формулировка)

| Команда | Пример формулировки | Эффект (гарантия кодом) |
|---|---|---|
| enable autonomy | «работай автономно», «приступай» | permission on, epoch+1; очередь идёт только в области workdir контракта |
| pause autonomy | «пауза», «замри, обсудим» | epoch+1 → новые автономные диспетчи block; идущему процессу interrupt_requested → подтверждение interrupted |
| resume autonomy | «продолжай автономно» | permission on, epoch+1 (старые approvals умирают) |
| cancel task | «отмени задачу task-xxxx» | task.state=cancelled, run остаётся interrupt_requested отдельно |
| agree task | «иди task-xxxx», «go task-xxxx», «dispatch task-xxxx» | MATERIAL согласован (agreed_at), draft→ready; pump диспатчит при включённой автономии scope. Голый «го/иди» без id — не команда |
| show status | «покажи статус задач» | список task/contract_version/permission |

Пауза не запрещает отдельный явно запрошенный interactive-шаг и не возобновляет очередь.

## Слэш-команды этого форка

Полный реестр (унаследованные + форковые) — `website/docs/reference/slash-commands.md`.
Ниже — только те, чего нет в upstream Hermes. Все работают в gateway-сессиях.

| Команда | Синтаксис | Эффект |
|---|---|---|
| `/for` | `/for <A> in <набор> [инструкция [--A...]] [--limit N] [--go]` | Принуждение множества через очередь контроллера: `<набор>` — директория, glob, литерал `{a, b, c}` или файл-список `@list.txt` (по item на строку); `<A>` — имя связи: заголовки шагов биндят его, в инструкции `<A>` разрешается ровно в item текущего шага; старая форма `/for <путь|glob> [инструкция]` тоже парсится. Итемизация → одна draft-задача, `ctl_step` на каждый пункт, приёмка каждого шага судом. Workdir получает autonomy permission (`granted_via=user-command`) — сам факт ввода команды есть грант. В контракт вшит запрет батч-скриптов по каталогу: цикл — это очередь, не скрипт. Без `--go` задача ждёт «иди tid»; с `--go` согласие встроено в командную строку (contract agreed, draft→ready). Гейды `ready→running` (MATERIAL + epoch) проверяются насосом на старте. Лимит 200 пунктов по умолчанию, максимум 1000. |
# Git-коррупция основного репо /home/qwen/hermes-autonomous-agent — 12.11

## Симптомы
- Любая команда git завершает auto-gc: `fatal: bad tree object <sha> · failed to run repack/prune`, пишется `.git/gc.log`.
- `git fsck --no-dangling` rc=10: десятки `missing tree/blob` (выборка: 141da074(t), 53ad8021(t), 000d03466c(t), 10c0a5aa(t), a210276a(t), 2f2c115f60(t), 9d80407(b), 59acc017(b), ...).
- Стартовое состояние: битый commit-graph (ссылался на нечитаемые коммиты f507944/f55385e2), два worktree-index cache-tree с невалидными sha (ha-sync, hermes-events).
- Повторный fetch с origin: `error: 3b7e77d0fa9c… did not send all necessary objects` — дыра есть и в истории на remote или в её выводе из локального графа.

## Что уже исправлено (12.11)
1. Устаревший commit-graph отложен: `.git/objects/info/commit-graphs` → `*.bak`.
2. cache-tree в worktree-индексах ha-sync и hermes-events самоперезаписан безопасно через `git status` в каждом worktree (staging их работ не тронут; HEAD/ветки читались только).
3. `git config gc.auto 0` — авто-gc отключён (реверсивно: вернуть `git config --unset gc.auto`). Без этого каждая git-команда шумела фаталом repack.
4. Полный `df`: на диске 276G свободно — не ENOSPC.

## Рабочее состояние
Коммиты/push/log работают на всех живых ветках (проверено: elk/dataset-export 1be1641ca2, elk/failure-analysis 1ddc2f522d запушены).

## Апдейт 12.11 (после первичной записи)
- `fsck`: 60 missing blob/tree, ВСЕ недостижимы из веток и reflog → история и пушнутый код целы; битое — только старые pack-объекты, которые полный repack всё равно перебирает и падает (`bad tree aa91493e…`, `Could not read 4150f883…`).
- Страховка исполнена 12.11: все 48 локальных веток запушены как `_backup/<имя>`, стэш «deploy-note scratch» → `_backup/stash-deploy-note` (проверить ls-remote при приёмке). После этого потеря мусорных объектов ничего не стоит.
- `gc.auto=0` остаётся до планового окна лечения (вариант A: свежий клон + перенос worktree-веток, чужие деревья — только по мандату).

## Не сделано (требует решения)
Восстановление недостающих объектов (refs-граф неполон частично; reflog-история могла прюниться в момент смерти предыдущего pack). Варианты:
- A: переразвёртывание репо через свежий клон + перенос уникальных worktree-веток (самое надёжное, но касается чужих worktrees — нужен мандат);
- B: точечная докачка деревьев/blob'ов с origin через GitHub API по списку missing из fsck;
- C: консервация как есть (gc.auto=0), репо работает, риск сохранить до планового окна обслуживания.


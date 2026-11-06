# Инцидент: внешние cron-воркеры лежали ~14 ч после переименования репо

Найдено тиком автономности 06.11 09:10. Исполнитель: @rtx5090 (этот инстанс).

## Симптомы

`cron.scheduler ERROR: Job '<id>': Restart-safe cron worker dispatch failed:
cron external worker exited before ownership acknowledgement (exit 1)` —
на каждой диспетчеризации всех external-worker задач (autonomy-tick
`9a7247c35d6f`, win-runner-recovery-watch `87291044a0f6`,
donor-reports-milestone `c1d920c4a6b3`). stderr воркера:
`pm.environments.NoCommittedEnvironment: no dependency environment is
committed for this install` (`cron/worker_bootstrap.py` →
`pm/environments.py::_require_own_dependencies`).

Хронометраж по journalctl: первая ошибка 05.11 19:25 (вечер переименования
`~/hermes-agent` → `~/hermes-autonomous-agent`), 532 ошибки за 14 часов,
последняя 06.11 08:58:15. Сам гейтвей жил и интерактивные сессии работали —
падал только первичный запуск fresh-воркеров.

## Причина

PMscoped-состояние лежит в `~/.hermes/installs/<sha256(resolved path of checkout)[:16]>`.
Переименование изменило resolved path → новый install_key
(`d0fe8fd6fc441cf5` вместо `7c40783fed4949c5`), а committed generation
осталась под старым ключом. Воркер бунтуется store-питоном
(`~/.hermes/tools/python-3.14.9...`), у которого нет своих пакетов, и
`_require_own_dependencies` честно отказывает. Сьюип переименования (05.11)
переписал systemd-юниты, jobs.json, watchdog-скрипты и worktree-указатели,
но не затронул `~/.hermes/installs/` — его и не надо переписывать, нужен
re-sync под новым ключом.

## Разрешение (не моё, побочный эффект)

06.11 08:58:18 деплой-цикл v0.2 (`gw-cycle-v02` по слову Вл в интерактивной
сессии) запустил `hermes update`, шаг «completing source-update dependencies»
создал committed generation под новым ключом (`facts.json` 08:58:21,
generation `29148858cf7a…`), гейтвей перезапущен 08:58:55. Дальше все
внешние задачи зелёные (win-runner-recovery-watch ok 09:07,
donor-reports-milestone ok 09:09; autonomy-tick — этот прогон).

## Механизм-долг (на решение Вл, кода не писал)

Разрыв обратимости: между переименованием и следующим update/restart вся
внешняя cron-механика мертва молча (тик-задачи не тикают). Кандидаты:
1. при `NoCommittedEnvironment` в worker_bootstrap — один lazy
   `hermes pm repair`-sync вместо отказа (риск: гонка сборщика, см. комментарий
   `activate_dependencies`);
2. либо алерт: `dispatch failed` подряд N тиков → durable-уведомление в
   Home-канал (у нас уже есть ctl_outbox pump для failed-RUN — тот же путь);
3. либо в процедуре переименования (host-ops/hermes-update скилл) — обязательный
   шаг `hermes pm repair` сразу после `mv`.
Минимально достаточный: (3) как правило + (2) как страховка. (1) трогает
boot-контракт PM — только по вердикту Вл.

## Проверено

- `NoCommittedEnvironment` в логах после 08:58:15 нет (окно до 09:12).
- jobs.json: enabled-задачи ok/streak 0; autonomy-tick `last_run` 08:58 (error,
  до фикса) → этот прогон.
- `installs/d0fe8fd6fc441cf5/facts.json` валиден, generation на диске,
  pyvenv.cfg на месте.

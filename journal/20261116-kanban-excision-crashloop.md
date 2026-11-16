# 16.11 — kanban excision: гейтвей-крашлуп (AttributeError) и двухстрочный фикс

## Фактура
- Краш-луп основного гейтвея (`hermes-gateway.service`, @rtx5090), по Вл окно 21:05–21:08 MSK, сработал systemd-бюджет рестартов (breaker).
- Корневая причина — недоделанная excision `b0a2b0da855` (kanban excision 2/3): методы `_kanban_notifier_watcher`/`_kanban_dispatcher_watcher` вырезаны из `gateway/run_watchers.py`, но их имена остались в списке спавна `_PRE_RECONNECT_WATCHERS` (`gateway/run_startup.py`) → `AttributeError` на каждом старте (getattr по не-existing-методу). Плюс `_kanban_busy()` импортировал уже удалённый `hermes_cli.kanban_db`.
- Обе kanban-БД пусты (досок в этом профиле нет) → корректное поведение `_kanban_busy()` = `return None`.

## Фикс (две строки Вл в моём checkout, НЕ коммитил → закоммитил я)
- `gateway/run_startup.py`: из `_PRE_RECONNECT_WATCHERS` вычеркнуть `"_kanban_notifier_watcher", "_kanban_dispatcher_watcher"` (два мёртвых имени).
- `gateway/run_idle_exit.py`: `_kanban_busy() -> None` (kanban вырезан; комментарий со ссылкой на b0a2b0da855).
- Не тронуто (оставлено на excision 3/3): `hermes_cli/slash_commands.py:392` (упадёт только при kanban-команде). `delegation_context` (session_context.py:90) под except — безопасно.

## Проверки (исполнение, не догадки)
- Все 4 оставшихся имени в `_PRE_RECONNECT_WATCHERS` существуют: `_session_housekeeping_watcher`/`_model_catalog_refresh_watcher`/`_heartbeat_watcher`/`_session_stall_watcher` → `gateway/run_watchers.py:40/214/232/283`. AttributeError не воспроизведётся.
- `grep -rn '_kanban_notifier_watcher\|_kanban_dispatcher_watcher' --include=*.py` → ноль. `kanban_db` в `gateway/` только в комментариях/docstring, живого импорта нет.
- `python3 -m py_compile` по 13 затронутым файлам + `session_worker_turns.py` → OK.
- Live: `systemctl --user` гейтвей `active (running)`, MainPID 3330, NRestarts=0, heartbeat свежий (Вл подтвердил pid==MainPID на 21:11). Экстренность снята. Простреленного рестарта не делал: экстренность снята, drain-aware-рестарт только по явному голу (fork-ops / R4).

## Состояние ветки (важно)
- Коммит двухстрочного фикса — только `run_startup.py` + `run_idle_exit.py` (самозавершённый, корректен поверх HEAD `b0a2b0da`).
- В рабочем дереве **осталась недоделанная excision 3/3** (~567 строк по 11 файлам): удаление `gateway/session_kanban.py`, вынос `run_worker_turns` → `gateway/session_worker_turns.py`, чистка `agent/*`/`hermes_cli/*`/`model_tools.py`, плюс **НЕзакрытые** `slash_commands.py:392` и ~8 test-файлов, всё ещё импортирующих удалённый `gateway.session_kanban` (запрокинутся) + 1 `RET501` в `session_managed_worker.py`. Этот блок я НЕ коммитил — он незавершён (сломанные тесты). Доводка 3/3 = отдельно.

## Ограничения
- Точная процессная хронология краш-лупа (окно 21:05–21:08 против ExecMainStartTimestamp 13:08:55) не сверялась на уровне живого процесса (live-интроспекция не делалась); зафиксированы только: корневая причина, фикс в источнике, и текущее live-состояние (active, NRestarts=0).

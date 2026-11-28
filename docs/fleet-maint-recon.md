# Разведка Д1–Д4 — fleet/upstream-sync@bd5eea7fc96 (elk/fleet-maint)

*28.11, агент-хранитель. Рабочий документ; итог уйдёт в CHANGELOG-fleet.md/отчёты.*

- Ветки: origin/main +5938, fleet/upstream-sync +15 относительно общей базы. Фичи — elk/* (этот документ и тесты — elk/fleet-maint от upstream-sync).
- **Д1**: `gateway/slash_commands.py:130 _spawn_detached_update` — отсоединённый путь **есть** для `/update`-команды. Инцидент 28.11 (Win) — агент вызвал `hermes update` **через terminal**, минуя /update; отрестартил гейт и убил собственный ход. Зона правки: детект «update изнутри хода» (env наследования гейта) + hand-off/yield. `hermes_cli/update_cmd_windows.py` уже знает про «detached delivery» exempt для `--gateway`.
- **Д2**: примитивы: `gateway/lifecycle_ledger.py` (sentinel unclean-exit), `run_watchers._heartbeat_watcher` (15s touch `$HERMES_HOME/gateway-heartbeat`), `state/gateway.heartbeat` (get_loop_heartbeat_path), `shutdown_watchdog.py` (heartbeat для внешнего надзирателя; stale = unknown, не dead). Не подтверждено: авто-рестарт при протухании + сохранение очереди входящих.
- **Д3**: копия `agent/turn_failure_copy.py:56`; эмитент границы — `gateway/run_turn_persistence.py:260` (один владелец boundary copy). Есть `failure_retryable` флаг и ручной /retry; автоповтора хода нет — это деликатная зона (риск двойного исполнения).
- **Д4**: matrix adapter `_is_duplicate_event` (plugins/platforms/matrix/adapter.py:803) — **in-memory** deque/set(maxlen 4096). Перезапуск гейта обнуляет память → replay событий даёт дубли в state.db (gap 0 с). Канон места дедупа — `gateway/session_ingress.py` (docstring), но persistent-дедупа по platform_message_id нет.

## Тестовая база
- `uv run --no-project --with pytest -- python -m pytest <файлы>` (python 3.14 в venv не годится).
- state.db тестов — через SQLite backup API не трогать живой; интеграционные тесты на временном HERMES_HOME.

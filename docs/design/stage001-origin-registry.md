# stage001 п.3 — Origin-реестр комнаты: заметки реализации

## Что сделано

Ядро больше не отвечает на вопрос «эта сессия — из этой комнаты?». Вопрос вынесен за границу
слоя: guard `/resume` (`gateway/slash_commands_session.py::_resume_access_denied_reply`) опрашивает
платформенный слой вызывающего и не содержит ни одной ветки по enum платформы (аксиома 2).
Ветка `if source.platform == Platform.MATRIX` удалена; временная заплатка `787938b895`
(lineage-разрешение через `gateway.branch_prefix._lineage_ids` внутри guard'а) отозвана целиком:
удалён вызов из guard'а, удалён параметр `current_session_id`, убран аргумент в точке вызова
`_handle_resume_command`. Сам `_lineage_ids` на месте — маркировка консолей (`⎇ <имя>:`) им
пользуется и дальше; тесты на инварианты helper'а сохранены.

## Контракт слоя (сигнатура хука)

Слой объявляет ответ на «принадлежит ли эта консоль моему чат-пространству»:

- на живом адаптере — метод
  `owns_session(source: SessionSource, session_id: str, *, origin: SessionSource | None, name: str, allow_cross_room: bool) -> bool | str | None`
- на точке объявления слоя — `register()` платформы — аддитивное поле реестра
  `PlatformEntry.owns_session_fn` с той же сигнатурой (без `self`); используется, когда живой
  адаптер недоступен (офлайн-бот, тестовый runner).

Вердикт: `True` — разрешить; непустая `str` — отказ, текст которого слой сам подставляет из
своих ключей i18n (`gateway.resume.matrix_blocked_*` — переезд текстов в слой — ряд A3, не этот
этап); `False` — доказано чужое (generic-отказ `gateway.resume.blocked_not_owner`); `None` или
что угодно нетипизированное (включая MagicMock в фикстурах) — «слой не ответил», ядро уходит в
платформо-независимую проверку `_resume_target_allowed`, которая и так fail-closed. Иерархия
разрешения хука: живой адаптер (`_delivery_adapter_for` → карта `adapters`) → запись реестра
платформ. Любое исключение внутри хука — как «нет ответа» (logger.debug), guard не падает.

## Неизвлекаемый origin (данные реестра)

Реестр живёт в индексе маршрутизации (`gateway/session.py::SessionStore`), потому что момент
«создание сессии» знает только он:

- `_origin_ledger: session_id -> SessionSource`, регистрация при создании маршрута
  (`_route_create`), при любом репоянте (`_replace_route_locked`, то есть `switch_session` —
  форк/резюм/автосброс) и при восстановлении маршрута (`_route_recover`), плюс посев из
  загруженных строк state.db и legacy sessions.json.
- Первое регистрированное значение канон: `setdefault`-семантика, репоянт не может «пережечь»
  происхождение кросс-румным `/resume --cross-room`.
- Ничего из switch/fork/resume-pruning ledger не дропает. Персистентность — meta-ключ
  `gateway_session_origin:<session_id>` routing-стора (тот же `set_meta/get_meta`, что у chat
  labels): переживает рестарт; при JSONL-фолбэке — только в процессе, что честнее молчаливой
  потери.
- Публичная сторона контракта: `lookup_session_origin(session_id)`;
  `AsyncSessionStore.__getattr__` отдаёт её автоматически. `_gateway_session_origin_for_id`
  теперь сначала живой routing-entry, потом ledger, и всё строго через
  `isinstance(..., SessionSource)` — моки-источники больше не проходят как origin.

## Почему lineage-заплатка больше не нужна

Форк-свитч ev-айтил routing-запись родителя, и `_gateway_session_origin_for_id(parent)`
возвращал None → «no recorded room origin». Теперь origin родителя зарегистрирован при его
создании и несъёмен, lookup отвечает из ledger, Matrix-слой говорит «своя комната» — `/resume
main` из дочерней консоли разрешён без всякого lineage-разрешения в guard'е. IDOR сохранён:
чужая комната даёт строку-отказ, незнакомый id без origin — отказ fail-closed.

## Telegram/Discord и прочие

Для платформ без `owns_session`/`owns_session_fn` путь guard'а бит-в-бит прежний:
`_resume_target_allowed` с тем же `allow_override=(allow_all or allow_cross_room)` и тем же
текстом. Единственное видимое усиление: после смены ключа origin больше не «теряется» при
eviction (ledger), то есть старые легитимные резюмы могут становиться разрешённее, а не строже;
случаев, где прежний код пропускал бы то, что теперь блокируется, нет: блокировка по
чужому origin/чужому chat проверяется точно так же.

## Тесты

`tests/gateway/test_resume_console_lineage_guard.py` переписан под новый контракт (покрытие
old-заплатки не удалено, а перенесено): (a) fork-ребёнок резюмит корневую консоль после
eviction записи switch_session; (b) чужая комната заблокирована + незнакомый id fail-closed;
(c) origin переживает switch/fork/резюм-ротацию, не пережигается репоянтом, читается после
«рестарта» из meta. Плюс: guard не содержит Platform.MATRIX/_lineage_ids и параметра
current_session_id; hook объявлен в register() Matrix-слоя; нетипичный verdict (MagicMock) —
не авторитет; фикстура `test_matrix_project_context_isolation` переведена на живой контракт слоя.

## Дальше (не этот этап)

- A2 (`_resume_row_visible`) и A3 (Matrix-тексты в generic-коде) — отдельные ряды
  `docs/design/stage001-platform-leaks.md`, к п.3 не относятся и остались как были.
  ✅ ЗАКРЫТЫ 31.10 (Вл «убрать в ядре зависимость от комнат матрикс»): ряды A2/A3 закрыты тем же
  слоем-хуков механизмом — общий резолвер `gateway/platform_layers.py::PlatformLayerHooksMixin`
  (`_layer_hook` → метод живого адаптера → map → `<name>_fn` из `register()`); листинг-видимость
  (`_resume_row_visible`) спрашивает `owns_session`; платформа-specific тексты `/resume`/`/sessions`
  идут через `resume_text_fn` (Matrix: `_resume_layer_text`, ключи cross_room_success/list_empty/
  list_row), блок scope `/status` — через `describe_scope_fn` (Matrix: `_matrix_describe_scope`,
  `_matrix_session_scope`/env читает сам слой). Из generic-кода удалены `_same_matrix_room`,
  `_redact_matrix_session_key`, `Platform.MATRIX`-ветки и чтения `MATRIX_SESSION_SCOPE`;
  в slash-модулях ядра нет ни одного вхождения «matrix» (пин
  `test_no_platform_branch_in_listing_and_wording_paths`). Поведение для платформ без хуков не
  изменилось: generic `_resume_target_allowed` / generic-тексты.
- Live-обкатка п.5 флота: после деплоя ленты комнат должны показать `/resume main` из ветки
  без refusals; натуральное подтверждение — там.

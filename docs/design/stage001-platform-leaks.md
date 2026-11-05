# stage001 п.1 — Инвентаризация протечек платформ в generic-ядро

Ревизия `fleet/upstream-sync @ 1c77334af1+`. Критерий row'а: platform-специфика **внутри generic-механизма**
(идентичность/роутинг сессий, ingress-admission, доставка ответа, контекст сессии). Таблицы реестра
адаптеров (токен-имена env, маппинг enum→класс, credential-гейт запуска) — слой B: тоже перечислены,
но это механический перенос деклараций в `PlatformEntry`, а не архитектурная протечка.

**Исключено как законный дом платформенного слоя** (проверено grep'ом, почему):
- `gateway/platforms/**` — базовые классы адаптеров и общие ingress-хелперы (`base.py`, `helpers.py`,
  `webhook*`, `api_server*`); знание о платформе здесь — их работа. Оговорка: `base.py` содержит и
  generic-механику (`merge_pending_message_event`, pending-слоты), в которую core лезет через
  приватные атрибуты адаптера — см. row B4.
- `gateway/relay/**` — транспортный адаптер (фронтит N платформ; `adapter_discord_interaction.py` держит
  Discord-знание внутри адаптерного слоя — допустимо).
- `plugins/platforms/**` — сами адаптеры (matrix, telegram, discord, slack…). Оговорка: у telegram уже
  есть `telegram_topic_routing.py`/`telegram_held_inbound.py` в плагине — трети topic-механики не хватает
  в слое, она живёт в ядре (секция A), это дублирование контракта.

Соглашение: `Platform.LOCAL`/`API_SERVER`/`RELAY/WEBHOOK` — классы поверхности (CLI/API/транспорт),
не внешние мессенджеры; их ветвление в admission (row D1) помечено отдельно, к аксиоме 2 относится
частично (RELAY — топология транспорта, LOCAL — сам агент).

---

## A. Идентичность и роутинг сессий (resume/fork/join/handoff, session key)

**A1. Location: gateway/slash_commands_session.py:305-311, 1047-1073 (`_same_matrix_room`, `_resume_access_denied_reply`)**
Знание: `/resume`-guard знает «комнату Matrix», thread_id-как-часть-ключа и lineage-разрешение из
заплатки `787938b895` («no recorded room origin» при eviction родителя форк-свитчем). Ветка
`if source.platform == Platform.MATRIX` прямо в generic-guard'е IDOR.
Владелец: Matrix-слой (origin-реестр консолей, stage001 п.3).
Способ удаления: guard задаёт слою вопрос «эта сессия — оттуда (та же комната/lineage)?»
(`platform_layers.proves_same_origin(current_source, target_session_id) -> bool|None`, fail-closed при
отсутствии слоя); `Platform.MATRIX`-ветка и lineage-разрешение удаляются, заплатка 787938b895 отзывается,
IDOR-тест «stranger block» остаётся.

**A2. Location: gateway/slash_commands_session.py:414-422 (`_resume_row_visible`)**
Знание: перечисление сессий «room-scoped только у Matrix», у остальных — generic-owner-проверка.
Владелец: тот же origin-слой (любая платформа с shared-контейнером: Matrix-комнаты, позже — общее
Discord-форум-сообщество).
Способ удаления: единый вопрос слою `layer.scopes_listing(source) -> Predicate[row]`; отсутствие слоя =
generic-владелец. Никакого enum-сравнения в ядре.
✅ Закрыто (31.10): `_resume_row_visible` спрашивает `owns_session` (тот же хук, что guard) — True
видно, строка-отказ/False — скрыто, нет ответа → generic `_resume_target_allowed`. `_same_matrix_room`
удалён из ядра; thread/room-семантика целиком в `_proves_same_origin` слоя.

**A3. Location: gateway/slash_commands_session.py:1146-1150, 1164-1165, 1171-1174; gateway/slash_commands_status.py:303-315**
Знание: Matrix-специальные тексты resume («cross-room success», «no named sessions», подпись комнаты в
списке) и блок `/status` «Matrix scope» с чтением приватного `_matrix_session_scope` адаптера и
`MATRIX_SESSION_SCOPE` env.
Владелец: Matrix-слой — статусные/поясняющие строки о собственной топологии.
Способ удаления: hook `layer.describe_scope(source) -> Optional[list[str]]` и `layer.resume_notes(...)`;
generic-код печатает то, что вернул слой; `getattr(adapters.get(Platform.MATRIX), '_matrix_session_scope')`
уходит вместе с приватным полем.
✅ Закрыто (31.10): два хука объявлены в `register()` — `resume_text_fn` (ключи `cross_room_success`,
`list_empty`, `list_row`) и `describe_scope_fn` (`_matrix_describe_scope`, читает `_matrix_session_scope`
/ env уже сам слой). Ядро печатает вернутое слово в слово; `_redact_matrix_session_key`, `hashlib`/`os`
и все `gateway.resume.matrix_*`/`gateway.status.matrix_scope_*` ссылки вынесены из generic-кода в слой.
Резолвер хуков — общий (`gateway/platform_layers.py::PlatformLayerHooksMixin`), ветки по enum платформы
в `/resume`, `/sessions`, `/status` больше нет (пин: test_no_platform_branch_in_listing_and_wording_paths).

**A4. Location: gateway/slash_commands_session.py:826-870 (`_handle_topic_command`)**
Знание: команда `/topic` целиком — Telegram-forum-механика (DM-gate `platform != TELEGRAM or chat_type != "dm"`,
BotFather-скриншоты capability-hint, `enable_telegram_topic_mode` в SQLite) живёт в generic-обработчике
slash-команд и тащит за собой половину run_topics.py.
Владелец: telegram-слой (`plugins/platforms/telegram/`), где уже живут topic-модули.
Способ удаления: регистрация команды слоем через plugin-command registry (ядро только диспатчит); state
topic-режима — в таблице слоя; `t("gateway.topic.*")` переезжает со строками.

**A5. Location: gateway/run_inbound.py:872, 1337-1341; gateway/run_topics.py:80-128; gateway/run.py:3660**
Знание: ingress-admission generic-потока спрашивает «это Telegram topic lobby?» и молча съедает сообщение
(или отвечает lobby-напоминанием с дебаунсом 30с), а `/new` в lobby подменяется сообщением вместо подтверждения.
«Топик» — слово, которого ядро знать не должно (аксиома 2).
Владелец: telegram-слой (lobby — свойство его topic-mode).
Способ удаления: pre-admission hook слоя `layer.intake(source, event) -> EventDecision(consume|rewrite|pass, reply)`;
ядро вызывает hook у слоя маршрутизируемой платформы и не знает, что hook телеграмный.

**A6. Location: gateway/run.py:3658-3679 (`_TELEGRAM_GENERAL_TOPIC_IDS`, `_normalize_source_for_session_key`); gateway/run_topics.py:174-235 (`_recover_telegram_lobby_thread_id`)**
Знание: **платформа переписывает thread_id источника до вывода session key** — lobby--shaped реплики
Telegram pin'ятся к последнему связанному топику. Ключ сессии (generic-идентичность) получает
телеграм-зависимую нормализацию; каждое site'а, выводящего ключ из raw source, обязана это повторять
(документированный баг #30479 — прямое следствие).
Владелец: telegram-слой как канонический нормализатор own-sources; contract — `layer.canonical_source(source)`.
Способ удаления: адаптер (слой) проставляет нормализованный thread_id при ingest (у telegram уже есть
adoption-логика в `telegram_topic_routing.py` — дополнить), ядро удаляет `_normalize_source_for_session_key`
и становится слепым к shape ключа; инвариант «source, пришедший с адаптера, уже каноничен».

**A7. Location: gateway/run_turn_prepare.py:310-313, 373-425 (`_hmwa_heal_telegram_topic_binding`); gateway/run_turn_runner.py:1113-1122**
Знание: подготовка turn'а и post-compression persistence зализывают telegram topic bindings
(`get/record/sync_telegram_topic_binding`), отдельная ветка «Telegram DM без thread_id → восстановить thread id».
Владелец: telegram-слой (его side-таблица, его восстановление после ротации).
Способ удаления: hook `layer.on_session_split(source, old_entry, new_entry)` / `layer.on_turn_prepare(source)`;
binding-таблица и её чтение уходят в плагин; ядро зовёт hook для всех слоёв без enum-проверки.

**A8. Location: gateway/slash_commands_branch_thread.py:20 (`BRANCH_THREAD_PLATFORMS = frozenset({DISCORD, TELEGRAM, SLACK})`), 37-75**
Знание: `/branch` создаёт sibling-thread только на «платформах, переопределивших create_handoff_thread»,
перечисленных руками; дальше — форма ключа каждого адаптера: у Discord inbound-тред имеет
`chat_id == thread_id` и sibling вешается на `parent_chat_id`; у Telegram ключ `group:<chat>:<topic>`;
у Slack — тип родительского канала + workspace scope; Matrix сознательно вне (docstring).
Владелец: capability адаптеров + форма source — объявление самих слоёв.
Способ удаления: `parent = adapter.host_thread_parent(source)` и `dest = adapter.thread_dest_source(source, parent, thread, title)`
(базовый класс возвращает None → ветвление in-place); frozenset удаляется — «кто переопределил хук, тот и
поддерживает». `format_thread_ref` — capability `adapter.render_thread_ref(thread_id)`.

**A9. Location: gateway/run_startup.py:1806-1840 (`_handoff_resolve_destination`)**
Знание: CLI→platform handoff при resume роутинга **зеркалит те же самые ключевые shape'ы** адаптеров
(Telegram-приватный чат как `dm` с `user_id == chat_id`, `looks_like_telegram_private_chat_id`,
Telegram forum → `group`, Slack → `D…`?dm:group + scope, Matrix → тип комнаты из get_chat_info,
Discord → chat_id = thread id). Дублирует A8 — два места, где ядро знает ключ каждой платформы.
Владелец: адаптеры (тот же `thread_dest_source`-контракт из A8).
Способ удаления: handoff просит целевой адаптер собрать source: `dest_source = adapter.handoff_source(home, title)`;
ядро не конструирует `SessionSource` для чужой платформы.

**A10. Location: gateway/session.py:690-743 (`build_session_key`, `_canonical_participant`)**
Знание: единственный источник истины ключа сессии содержит три платформы-патча: WhatsApp JID/LID
каноникализация участников и DM chat_id (694-695, 713-714), Discord auto-thread continuity через
`prospective_thread_id` с нормализацией слота chat_type (715-719), Slack `scope_id` в сегменте ключа
(732-733, с закомментированным исключением Discord guild).
Владелец: capability `adapter.canonical_participant/canonical_chat/canonical_thread(source)` и
`key_segments(source)` — объявление слоёв.
Способ удаления: ядро собирает ключ из канонических сегментов, возвращённых слоем; WhatsApp/Discord/Slack
патчи переезжают в их плагины; байт-овверлей ключей фиксируется регресс-харнесом (контракт совместимости).

**A11. Location: gateway/session_recovery.py:100-132 (`_legacy_slack_session_key`, `_recovered_row_matches_source_scope`)**
Знание: восстановление сессий после рестарта знает про workspace-scop'ы Slack, «legacy» ключи без scope_id
и миграционную claim-логику; generic-проверка «строка принадлежит источнику» специализирована под один enum.
Владелец: Slack-слой (его историческая миграция) + generic-контракт scope-совпадения.
Способ удаления: перенести в плагин Slack как hook `layer.recovered_row_ok(row, source)` и
`layer.legacy_keys(source)`; ядро опрашивает слой источника; когда миграция догорит — hook удалится вместе со слоем.

---

## B. Ingress-обработка и доставка ответа

**B1. Location: gateway/run_inbound.py:1602-1632 (`_prepend_inbound_reply_context`)**
Знание: budget-квота на цитируемое сообщение **только для Matrix** (`[:500]`, комментарий RECEPT-reply-context-v2 C5:
у Telegram «письменный контракт» на целые цитаты) + Discord triggering-message note как outermost-префикс
с парным стриппером `strip_discord_triggering_note` (контракт против cache-busting).
Владелец: слои matrix/discord (лимиты цитат и якоря — их wire-контракты).
Способ удаления: reply-context собирается слоем: `layer.render_reply_context(event) -> Optional[str]` +
`layer.turn_prefix(event)`; ядро вставляет, не зная формата; budget — параметр слоя.

**B2. Location: gateway/run_inbound.py:1441-1449**
Знание: в shared multi-user сессиях speakers-prefix получает `<@U…>` Slack user id (ссылка #17916) —
platform-specific способ предъявить верифицируемый id отправителя.
Владелец: Slack-слой (упоминания — его лексикон).
Способ удаления: `layer.speaker_identity(source) -> str` — generic-префикс `[name]`, слой дописывает свой id.

**B3. Location: gateway/run_inbound.py:669-691 (`_hm_busy_telegram_grace_queue` + env `HERMES_TELEGRAM_FOLLOWUP_GRACE_SECONDS`)**
Знание: generic busy-preempt путь делает исключение «Telegram TEXT в грейс-окне не прерывает turn, а
кввится» — platform+message_type ветка и platform-имя в имени env-переменной.
Владелец: telegram-слой (это tuning его flood-control поведения).
Способ удаления: capability-запрос до прерывания: `layer.prefer_queue(event, turn_state) -> bool`; config
переезжает в `platforms.telegram.extra`.

**B4. Location: gateway/run_inbound.py:595-626 (`_hm_busy_slash_or_photo`, комментарий «Telegram photo bursts…»); gateway/session_ingress_burst.py:40, 84, 122**
Знание (media-hold fusion + text-batch debounce): generic burst-механизм ядра дергает приватные методы и
поля адаптера — `_is_queue_text_debounce_candidate`, `_can_merge_text_debounce_events`,
`adapter._pending_messages`, `_busy_text_debounce_seconds`, `_busy_text_hard_cap_seconds`. Семантика
«альбом merge'ится, текст дебаунсится» родилась из Telegram-поведения, но код уже почти generic —
контракт неформальный (по имя-с-подчёркиванием).
Владелец: базовый адаптер (объявить публично) + настройки timing у слоя.
Способ удаления: формализовать public capability-интерфейс в `BasePlatformAdapter`
(`can_merge(head, event) -> 'text'|'photo'|None`, `merge(head, event)`, `debounce_window()`), burst-код ядра
перевести на него; приватные имена удалить. Это «способ удаления» без выселения из ядра — знание уже
обобщено, нужна лишь граница.

**B5. Location: gateway/run_inbound.py:1186-1217 (`_hm_rescue_orphaned_fifo`)**
Знание: reassembly batch-очереди (orphan rescue) пересаживает turn на source спасённого события, «чтобы
reply anchors / thread metadata указывали на отвечаемое сообщение». Проверено: веток по платформам **нет** —
механизм чист; протечка опосредованная — корректность reassembly держится на том, что shape platform-полей
(thread_id, reply-якоря) в `source` совпадает с тем, что ожидает доставка конкретной платформы (A6/A8-A10).
Владелец: после A6/A8-A10 — слой, гарантирующий каноничность source.
Способ удаления: отдельного удаления нет; регресс-тест «orphan rescue сохраняет provenance → thread
metadata совпадает» на 2+ платформах, чтобы формализовать контракт вместо молчаливой зависимости.

**B6. Location: gateway/run_busy.py:581-587 (`_busy_reply_to`)**
Знание: правило якоря busy-ответа: у Telegram DM-топика — anchor на thread, остальные Telegram-треды —
без якоря; у прочих платформ — ответ на message_id.
Владелец: telegram-слой + generic-контракт reply-якорей.
Способ удаления: `adapter.reply_anchor_for(event)` — базовая реализация = текущее generic-правило,
Telegram переопределяет.

**B7. Location: gateway/run.py:477-484 (`_non_conversational_metadata`), 3873-3953 (`_thread_metadata_for_source` / `_thread_metadata_for_target` / `_is_telegram_dm_topic_target`)**
Знание: generic-построитель metadata для **любого** synthetic send знает: telegram-ключи
(`telegram_dm_topic_reply_fallback`, `direct_messages_topic_id`, `telegram_reply_to_message_id`),
Slack-ключи (`slack_team_id`, `recipient_user_id`, `message_id` как различитель тредов), Discord-маркер
`non_conversational`, и лезет в `_get_dm_topic_info` класса чужого адаптера через `getattr(type(adapter), ...)`.
Владелец: адаптеры — `adapter.thread_metadata(target, reply_to)` (базовый = `{thread_id}` + reply).
Способ удаления: ядро вызывает конструктор метаданных у целевого адаптера и только дописывает свои
route/profile поля (`source_route_metadata`, `hermes_profile`); все platform-ключи рождаются в слоях.

**B8. Location: gateway/run_turn.py:1095-1109, 1175-1261 (`_thread_metadata_for_progress` и inline-progress)**
Знание: доставка прогресса различает Slack/Buzz (`_reply_to_mode`, `_effective_reply_in_thread`,
`reply_in_thread` extra), relay-Discord (`prospective_thread_id` при `delivered_via_upstream_relay`),
Feishu/Mattermost (thread_id + reply_to_message_id в status-метаданных), native Slack task cards
(`recipient_team_id`/`recipient_user_id`). Строковое сравнение `== "buzz"` — ещё и мимо enum.
Владелец: слои slack/buzz/feishu/mattermost/discord-relay.
Способ удаления: единый capability `adapter.plan_progress_delivery(source, event_id) -> (metadata, reply_to, status_meta)`
(базовый = текущий generic default); platform-ветки удаляются из ядра; `is_buzz`-строка умрёт вместе с ними.

**B9. Location: gateway/run_turn.py:740-767 (`_build_stream_config`)**
Знание: stream-контент-правка под платформы: Telegram — pause typing closure и fresh-final только для
Telegram; Matrix — пустой курсор («клиенты рисуют tofu»); у QQ/WeChat «не редактируют» уже через capability
(`SUPPORTS_MESSAGE_EDITING`) — образец, к которому надо привести остальные ветки.
Владелец: адаптеры (курсор, typing, fresh-final — wire-поведение).
Способ удаления: `STREAM_CURSOR`/`FRESH_FINAL` поля в PlatformEntry/адаптер + capability-колбэк
`on_stream_finalize(source)` вместо `hasattr(adapter, 'pause_typing_for_chat')`.

**B10. Location: gateway/delivery.py:86, 300-320 (`looks_like_telegram_private_chat_id`, `_ensure_named_dm_topic`)**
Знание: generic-маршрут доставки **создаёт Telegram-топики** (`createForumTopic` по name-thread), решает,
что numeric topic-id без reply-якоря недоставляем (raise), и опознаёт приватные Telegram chat_id по
математике отрицательных id. Delivery-роутинг ядра знает Telegram ID-лексику.
Владелец: telegram-адаптер (`adapter.resolve_send_target(target)`).
Способ удаления: доставка спрашивает у адаптера целевой lane: `adapter.prepare_send(target, metadata) -> metadata'`;
хелперы `looks_like_*`/`_ensure_named_dm_topic` переезжают в плагин telegram.

**B11. Location: gateway/run_busy.py:1503-1546 (`_is_stale_restart_redelivery`)**
Знание: дедупликация /restart по `update_id` — чисто Telegram-семантика (числовая монотонность, маркер
`.restart_last_processed.json` с platform-строкой), оформлена как generic-гейт с двумя строковыми
сравнениями `platform.value != "telegram"`.
Владелец: telegram-слой (его update_id — его гарантия) + generic-контракт «адаптер сам дедуплирует доставки».
Способ удаления: хелп-хук `adapter.is_redelivery(event)`; по умолчанию False; маркер пишет telegram-плагин.

**B12. Location: gateway/run_shutdown.py:152-163 (`_delivery_target_key`)**
Знание: dedup-ключ рестарт-уведомлений делает исключение «Telegram positive chat id = пользователь, не чат»
(импорт `looks_like_telegram_private_chat_id` из delivery, строковое `== "telegram"`).
Владелец: telegram-слой (та же ID-лексика, что B10).
Способ удаления: `adapter.target_dedup_key(chat_id, thread_id)` — дефолт generic; телеграм-исключение в слое.

---

## C. Системный контекст сессии (что видит модель)

**C1. Location: gateway/session.py:420-435 (`build_session_context_prompt`)**
Знание: блок «**Matrix Room / Room ID / Thread / room boundary**» в generic-промпте контекста, включая
поведенческую инструкцию про границы комнаты. Ядро знает, что такое «комната» (аксиома 2).
Владелец: Matrix-слой.
Способ удаления: `layer.context_lines(source) -> list[str]` — слой дописывает свои строки в generic-секцию.

**C2. Location: gateway/session.py:189-199 (`_PII_SAFE_PLATFORMS`), 330-375 (`_PLATFORM_NOTES`, `_discord/_slack_platform_notes`, `_STATIC_PLATFORM_NOTES`), 451**
Знание: маппинг enum → текст/режим: какие платформы «PIX-safe», какие platform-notes вешать (Discord
«не обещай поиск по каналу», iMessage-«пузыри», Yuanbao-tool).
Владелец: соответствующие слои (заметки — часть authoring-контракта платформы, ADDING_A_PLATFORM).
Способ удаления: поля `pii_safe: bool` и `context_notes(ctx)` на PlatformEntry; ядро читает реестр,
словарей по enum в generic-файле нет.

**C3. Location: gateway/run_agent_cache.py:653-662 (`_voice_channel_sidecar_note`), 787-802 (discord/slack гейты в сигнатуре agent-cache); gateway/run_voice.py:168-311**
Знание: generic-кэш-подпись и sidecar-механизм содержат Discord voice-канал (обход `get_voice_channel_context`)
и gate Slack/Discord MCP-tools; run_voice целиком прописывает DISCORD-адаптер в generic voice-routing.
Владелец: discord-слой (voice — его фича); slack — его tool-note.
Способ удаления: sidecar-хук `layer.per_turn_note(source) -> Optional[str]` (уже есть shape «note на user
message» из C1); presence gate — из возвращаемых слоём тегов; voice routing — регистрация discord как
voice-provider в реестре, а не `adapters.get(Platform.DISCORD)`.

**C4. Location: agent/system_prompt.py:429; gateway/run_turn_persistence.py:176-180**
Знание: rich-message вставка для telegram прямо в generic system prompt; у Mattermost reasoning по умолчанию выключен
в generic persistence-пути (`require_platform_override_for={Platform.MATTERMOST}` — и в run_turn.py:1085-1089
для interim/thinking progress).
Владелец: telegram/mattermost слои (дефолты отображения — объявления платформы).
Способ удаления: default-override таблицы (`require_platform_override_for`) живут в PlatformEntry
(`display_defaults={...}`), generic-чтение `resolve_display_setting` их опрашивает; telegram-rich строку —
в note-хук слоя (C1).

---

## D. Admission / авторизация / транспортная топология

**D1. Location: gateway/session_ingress.py:206-213, 271-272; gateway/session_ingress_context.py:151-156**
Знание: advice-текст admission refusal выбирает `/hermes discard` vs `/discard` по строковому `platform.value == 'slack'`
(slash-cap платформы в generic-тексте); «не chat-route» определение — `platform == API_SERVER || LOCAL` (клейм
владельца turn'а); binding транспорта подменяет platform на RELAY. RELAY/LOCAL/API_SERVER — классы
поверхности, но их перечисление в generic-условиях — это knowledge-таблица без владельца.
Владелец: PlatformEntry-флаги (`umbrella_only_commands`, `is_chat_surface`, `transport_fronting`).
Способ удаления: булевы/перечислимые декораторы на записях реестра: `entry.no_native_slash('/')`,
`entry.surface in {chat, api, local}`; сравнения с enum исчезают из generic-кода, релокейт-логика RELAY уже
частично идёт через registry-хелперы.

**D2. Location: gateway/authz_mixin.py:35, 164-180, 702-710, 743; gateway/config.py:811-817**
Знание: allowlist-матчинг ingress principal'а знает JID/LID-алиасы WhatsApp (+ SimpleX npub→hex, Buzz),
legacy-грант `TELEGRAM_GROUP_ALLOWED_USERS` через chat-grant эвристику, env-имя `_GROUP_USER_ENV` для
Telegram; «Email — inbox-shaped → unauthorized DM дефолт ignore» продублирован в config-геттере.
Владелец: authz-контракт слоя: `layer.expand_principal(id) -> set[str]`, `layer.identity_aliases`,
dm-policy-объявление на PlatformEntry.
Способ удаления: альясы спрашивать у слоя платформы источника; env-таблицы — в PlatformEntry; email-ignore
дефолт — `dm_default="ignore"` на записи email-платформы. (Авторизация остаётся в ядре — IDOR-смысл,
аксиома 3; наружу уходит только platform-лексика идентичностей.)

**D3. Location: gateway/run_inbound.py:194-209; gateway/run_notifications.py:196-201; `_is_slack_ignored_channel` (gateway/run.py:1195)**
Знание: ignored-channels guard в начале generic intake проверен **только для Slack** (и повторно в notices),
хотя ignore-листы есть свойство любой группы-платформы.
Владелец: generic-механизм (per-platform config) — сейчас наоборот: generic-механизм скопирован под один enum.
Способ удаления: `config.is_ignored_channel(platform, chat_id)` + `adapter.ignored_channels()` — generic для
всех платформ; platform-ветка убирается, поведение Slack сохраняется конфигом.

**D4. Location: gateway/session_envelope.py:40-56**
Знание: generic projection «какие нативные метаданные адаптер имеет право положить в envelope» жёстко
замаплен на `platform.value == 'whatsapp'` и whitelist `whatsapp_native*`-ключей.
Владелец: контракт адаптера — `adapter.NATIVE_METADATA_KEYS` (объявление слоя), generic-продусер его читает.
Способ удаления: читать ключи/префикс с адаптера или PlatformEntry, проверить размер; ветка по enum исчезает.

---

## E. Командная поверхность и вспомогательные тексты

**E1. Location: gateway/slash_commands.py:163-172 (`_home_thread_from_source` — синтетический Slack thread_id), 645-650 (page_size=15 для Telegram); gateway/run.py:878-890 (`_telegramize_command_mentions`); gateway/run_turn_prepare.py:595 (`/hermes sethome` для Slack)**
Знание: форматирование командных ответов и /sethome-семантика знают ограничения Telegram bot-имён, длину
телеграм-страницы, Slack thread-per-message и slash-cap Slack.
Владелец: слои (rendering-контракт surface).
Способ удаления: `adapter.sanitize_command_text(text)`, `adapter.help_page_size`, `adapter.command_path(cmd)` —
базовые реализации = текущие generic defaults; вызовы без проверок enum.

**E2. Location: gateway/channel_directory.py:96, 144-147**
Знание: generic-директория каналов для Discord/Slack вызывает приватные билдеры `_build_discord`/`_build_slack`,
у остальных — session-discovery fallback; строковое `platform_name == "discord"` (96).
Владелец: адаптеры уже могут дать `list_channels()` (путь есть выше в том же цикле).
Способ удаления: перенести `_build_discord`/`_build_slack` в методы этих адаптеров (закрыть дыру
«generic-файл знает чужую структуру»), generic-цикл состоит только из `list_channels` + session fallback.

**E3. Location: gateway/run.py:3595, 3957-3963 (`_UPDATE_ALLOWED_PLATFORMS`), gateway/run_startup.py:1065**
Знание: allowlist `/update` и множество «не-messaging» платформ захардкожены публичными списками enum
(для плагинов механизм уже декларативный — `allow_update_command=True` на PlatformEntry, built-in'ы — нет).
Владелец: PlatformEntry-декларации built-in'ов.
Способ удаления: перенести два built-in-списка в записи реестра (`allow_update_command`, `surface`); generic-проверка читает entry.

---

## F. Инструменты и CLI (тоже «ядро» для агента)

**F1. Location: tools/send_message_tool.py:683-700 (цепочка weixin → Telegram → policy → generic; `_platform_max_length`); tools/send_message_senders.py:424 (live-адаптер Matrix ради E2EE); tools/send_message_targets.py:42 (формат matrix `!`/`@` target)**
Знание: standalone-доставка сообщения из generic-инструмента знает Telegram chunking/formatting, Matrix E2EE
re-init и синтаксис Matrix-адресов.
Владелец: send-контракт слоя (`standalone_send(platform, ...)`) + discovery live-адаптера через registry.
Способ удаления: dispatch на `policy.standalone_sender` (уже есть для плагинов) для всех; Matrix-хелперы
(`_matrix_send_core`, live-lookup) — в плагин matrix, вызов через entry hook.

**F2. Location: tools/cronjob_job_args.py:79 (Slack thread_id → `thread_ts`? special-case); tools/kanban_tools.py:1213 (Telegram topic thread_id → link-формат)**
Владелец: слои форматов ссылок/потоков.
Способ удаления: `layer.format_thread_target(thread_id) -> dict`/str; generic tools не смотрят в enum.

**F3. Location: gateway/run.py:1908-1935 (`_platform_has_bot_credential`: WhatsApp session-creds, Matrix password-trio из `extra`); gateway/config.py:354, 399-404; gateway/config_loader.py:200-201, 465-481 (Telegram/Signal coercions); gateway/config_env.py, gateway/platform_env.py (env-таблицы)**
Знание: generic reconnect/activation-цикл знает форму чужих credentials; loader правит platform-specific
слоты конфига.
Владелец: PlatformEntry (`credential_check(cfg)->bool`, `env_token_name`).
Способ удаления: declared checks в реестре (для плагинов механизм уже есть — `check_requirements`); built-in
записи получают те же колбэки; loader-коэрсии — в `on_config_load` слой. **Это самый массовый, но самый
безопасный класс: поведение не меняется, меняется адресат знания.**

**F4. Location: hermes_cli/cli_info_lines.py:42-45; hermes_cli/web_routers/messaging.py:270, 1143-1144; hermes_cli/commands_platforms.py:265; hermes_cli/gateway_setup_wizard.py:159, 270; tools/environments/local_env_policy.py:250 (`== "buzz"`)**
Знание: setup-wizard/status/CLI-подсказки держат локальные таблицы platform → env/format.
Владелец: реестр платформ + сами плагины (wizard-prompt как entry-декларация).
Способ удаления: единый источник (registry metadata: token env, args-schema, wizard hints); строковые
сравнения `platform_id == "telegram"` в web-роутере заменяются на чтение entry.

---

## Проверенные якоря без собственной протечки

- **B5** (`_hm_rescue_orphaned_fifo`, run_inbound.py ~1195-1214) — reassembly provenance: платформенных
  веток нет, зависимость транзитивная (см. row).
- `gateway/hosted_room_*.py`, `session_local_*.py`, `session_worker_*` — grep чист: «room» там own-абстракция
  hosted-контроля, не платформа.
- `agent/vault_login_classifier.py:119` (`control.type == "email"`) — тип классификатора действия, не Platform.
- `Platform.LOCAL` в CLI-surface местах (run_turn_runner.py:160/196/285/375/585/1239, plugin_* ) — «агент на
  своей машине» — собственное понятие ядра; не считаем протечкой, каталогизировано в D1.

---

## Итог

**Всего протечек: 38** (A: 11, B: 12, C: 4, D: 4, E: 3, F: 4).

**Сводка способов удаления** (по классам):
- **Вопрос слоя (origin/ownership hook)** — A1, A2, A3: ядро спрашивает слой о происхождении сессии; заплатка 787938b895 отзывается на п.3.
- **Hook интейка/turn-подготовки** — A5, A7, B3, B11: `layer.intake` / `layer.on_session_split` / `adapter.is_redelivery`.
- **Канонизация источника на границе адаптера** — A6 (+регресс B5): source выходит с адаптера уже с правильным thread_id.
- **Capability-методы адаптера вместо frozenset/enum-веток** — A8, A9, A10, A11, B4, B6, B7, B8, B9, B10, B12, D1, D4, E1, E2.
- **Команда/текст в плагин** — A3 (status/resume строки), A4 (/topic), C1, C2, C4, F2: note/командный реестр слоя.
- **Декларации в PlatformEntry (реестр)** — D2, D3, E3, F3, F4: env-имена, credential-check, dm-политика, display-defaults.
- **Send/target контракты слоя** — F1, F2.

Порядок снятия (для п.3): сначала реестровые декларации (F3/E3 — безопасно, без изменения поведения),
затем origin-reестр и A1-A3 (это и есть stage001 п.3), затем thread-shape контракты A8-A10+A6 (общий
контракт формы ключа), далее delivery hooks B7-B10. A4-A7/B1-B3 тянутся за переездом telegram-topic в слой.

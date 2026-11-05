# Аудит: ядро resume не зависит от gateway-роутинга (birth-row priority)

## Контекст (коротко)
Живой баг: `/resume main` в Matrix-комнате другого экземпляра (@rtx5090) отвечал
«⚠️ Matrix /resume blocked: this named session has no recorded room origin».
Причина: guard resume-команды доказывал «этот чат — родной» по mutable-state гейтвея:
live routing entry (периорится switch_session/fork) и origin-ledger (посевается из живых
routing rows при загрузке). Ротированный форком родитель, рождённый до деплоя реестра,
не имел ни того, ни другого → fail-closed отказ.

Сделано два шага (ветка fleet/upstream-sync, локально, ещё не запушена):
1. `997c35ed0d` — A2/A3: из ядра slash-команд удалены Platform.MATRIX-ветки, matrix-тексты,
   `_same_matrix_room`, `_redact_matrix_session_key`, чтения MATRIX_SESSION_SCOPE. Введён
   hook-резолвер `gateway/platform_layers.py::PlatformLayerHooksMixin` (owns_session /
   resume_text / describe_scope), живой Matrix-адаптер реализует, platform_registry несёт
   `*_fn`-декларации из register(). Ответ слоя None → generic-путь ядра (guard fail-closed).
2. `71caefe06e` (+`0ed7f38e23`) — `_gateway_session_origin_for_id` перевёрнут: ПЕРВЫМ
   читается durable birth-строка state.db `sessions.origin_json` (пишется при ensure_session),
   затем advisory: live routing entry → store._entries → origin-ledger. Пин-тест:
   роутинг+ledger указывают Room B, birth-строка Room A → доказано Room A.
   Когорта 59/59 (guard+resume+изоляция) зелёная; широкая 265 — 8 падений identically
   на базе до моих коммитов (env-грязь).

План применения (утверждён владельцем «го», approve 6ч): пуш ветки в форк VovaElkTver;
на чужих деревьях ≥997c35ed0d — git apply чистого патча (проверено dry-run rc=0);
на старых p.3-деревьях — checkout ветки; холодный рестарт systemd-юнита;
read-back через Matrix client API (события комнаты), не глазами.

## Самокритика автора (что меня беспокоит)
a) Приоритет birth над роутингом может быть неверен для легитимной миграции: /move,
   миграция профиля, rekey_profile_routing меняют «живую» комнату — при конфликте
   guard станет отказывать в комнате, где сессия сейчас законно живёт, требуя --cross-room.
b) origin_json пишется ensure_session — не гарантированно «при рождении» для сессий,
   созданных путём без ensure (не проверял все пути создания).
c) Advisory-хвост (ledger/entry при отсутствующей birth) может вернуть CURRENT комнату;
   для сообщения «no recorded room origin» это редкий путь, но семантика «origin» размывается.
d) На чужом rtx5090-экземпляре поколение дерева НЕ верифицировано (я достал только
   предположение из наличия строки текста отказа); деплой-план может разойтись с реальностью.
e) Я не проверял, что birth-row-фикс вообще был нужен именно там (других экземпляров),
   а не только здесь: у меня локально тот же кейс закрыт посеянным ledger'ом (1287 записей).

## Вопросы
1. Последствия birth-priority: перечисли реальные сценарии, где durable-родитель против
   живого роутинга даст НЕВЕРНЫЙ отказ/пропуск, и оцени, нужен ли tie-breaker.
2. Безопасность: не открывает ли патч IDOR/cross-tenant брешь (фальшивый/грязный
   origin_json, session_id-перебор, shared-группы), которого не было при ledger-only?
3. Верно ли, что патч лечит исходный кейс до корня, или лечит лишь одно из звеньев
   (посев/ротацию)? Что останется сломанным после него?
4. Что не так в деплой-плане (пуш в форк, git apply vs checkout, холодный рестарт чужого
   гейтвея, верификация через Matrix API)? Назови пропущенные шаги и риски отката.

## Формат ответа
ВЕРДИКТ одной строкой (GO / GO-WITH-CONDITIONS / NO-GO), затем разбор по вопросам 1–4,
ссылки на места в тексте выше, без вежливости.

# rtx5000 matrix-dead (16.11 ~13:3x MSK)

## Фактура
- Unit hermes-gateway-dev активен (PID 3328, старт 13:08:55, NRestarts=0), heartbeat свежий — жив event loop.
- Мёртв только matrix: adapter hard-gate 'device WPALFGNQAR has stale one-time keys on the
  server signed with a previous identity key' — 6+ retry с backoff 300с, healable=False.
- Ошибки начались ~13:01 (до рестарта юнита); presence @rtx5000 offline с ~12:57.
- Фоновый шум (не причина): state.db behind SCHEMA_SQL event_observations/reconciliation_checks
  (NOT NULL без default).

## Блокер
- Лечение = delete device + новый login ИЛИ новый token. В ~/.hermes-dev/.env только
  ACCESS_TOKEN (привязан к WPALFGNQAR; device-delete убьёт его) + MATRIX_DEVICE_ID запинен;
  MATRIX_PASSWORD нет. Admin-token Synapse неизвестен где.

## План (после credentials от Вл: пароль / admin-token / выписанный токен)
1. login (или import выписанного токена) → ~/.hermes-dev/.env: MATRIX_ACCESS_TOKEN, MATRIX_DEVICE_ID
2. systemctl --user restart hermes-gateway-dev
3. Проверка: presence online, whoami, реплай @rtx5000 в комнате; только потом «жив».

## Наблюдение про наш форк
- task_queue в этой сессии: любой action (create_batch, status) → 'Tool handler returned
  unsupported result type: dict' — контрактный баг хендлера, не отсутствие данных.

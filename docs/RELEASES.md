# RELEASES — живой пин ноды rtx5090

## LIVE (обновлено 27.10 ~17:35 MSK)
- Checkout `~/hermes-agent`: `fleet/upstream-sync` @ `90c1ae8b7c`
  (мерж upstream 27.10 `ef5d89067a` + форк-слой: safe-mode, hermes_events, rescue, controller ticker, heartbeat)
- Interim-патч WSL (`start_writer`: `thread.start()` → `pass`) **снят с дерева в 17:28** кем-то внешним;
  живой процесс (PID 2160630, старт 16:59:38) держит патченную копию в памяти. Следствие:
  **следующий рестарт идёт по чистому HEAD и не проверен** — dry-boot перед циклом обязателен,
  правку в память не вносить без @vovak.
- Требуемая схема state.db: колонки hermes_events/lease-слоя (`event_id`) — при `no such column` откатывать checkout, не латать БД.
- Откатная ветка: `keep/live-snapshot-2410` @ `9c94291511` (запушена в origin).
- Санитар и протокол: `docs/reanimation-rtx5090.md` (@rtx5000).

## История
- 25.10 19:10 — rollback 5732b577 → `17e581e27b` LIVE-SNAPSHOT (тег `pre-rollback-5732b577`), state.db на чистую (1.1 МБ; старая в ~/state-side/, не возвращать).
- 27.10 10:30 — накат `90c1ae8b7c`, бут-луп (event_id / threads-once), кома до ~15:30; поднято WSL с interim-патчем.

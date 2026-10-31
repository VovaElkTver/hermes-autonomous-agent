deploy: enable controller ticker — merge elk/ticker-audit-fixes (16 commits, critic NO-GO→patched→GO-with-conditions) FF into fleet/upstream-sync; `hermes plugins enable controller`; gateway cycle внешним таймером
from: 42bc79aa5e3f27b30ffa51389e51a3f05e5f9f0d
to: c1169c1ffc04d34a6828f90461ceef995a15c1d0 (elk/ticker-audit-fixes tip == пост-merge fleet/upstream-sync)
risk: controller plugin на boot: ticker стартует резидентно; худший живой сценарий — crash-loop или молчаливый ticker (см. watchdog-условия re-check t20261028T214811). DDL ctl_ticker_heartbeat/ctl_alert_state/ctl_run.worker_pid ляжет на первом writable open. Откат: switch_release на from-SHA + убрать controller из plugins.enabled; триггеры и рука — rtx5000 (reanimation-ops), baseline-снимок у него.
who: @rtx5090 main gateway, по «иди» @vovak 28.10; исполнитель наката — @rtx5090, наблюдатель/спасатель — @rtx5000

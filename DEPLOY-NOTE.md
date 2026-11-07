# DEPLOY-NOTE (dev node rtx5000)
planned 2026-11-07 ~13:0x MSK by @rtx5000 session:
- operation: hermes update --branch main --yes in worktree ~/hermes-dev (switched feature/agency-toolset-v02 -> main first)
- target return SHA: origin/main 015d11a133 (local main == origin/main at op start)
- rollback: worktree back to branch backup/agency-v02-preupdate (= aecfe33ad6) + systemctl --user restart hermes-gateway-dev.service
- feature work preserved: branch feature/agency-toolset-v02 aecfe33ad6 (+ backup ref), deployed copies at ~/.hermes-dev/plugins/agency/
verified-alive criteria (fill after boot): Fleet version @ 015d11a133, MainPID != 2699912, 'agency: toolset registered', 'agency loop online'

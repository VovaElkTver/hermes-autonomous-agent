# node-ops.md — install / update / restart / rollback protocol for agents

Node lifecycle operations on hosts running this fork. Source of truth for
what the installers do: `fork-install.sh` / `fork-install.ps1` (repo root) and
`setup-hermes.sh` / `setup-hermes.ps1`. The human-facing mirror of this
protocol: `docs/humans.md`.

## Install

One-liner (Linux/WSL):

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/VovaElkTver/hermes-autonomous-agent/main/fork-install.sh)
```

Windows: `iwr -useb https://raw.githubusercontent.com/VovaElkTver/hermes-autonomous-agent/main/fork-install.ps1 | iex`

The installer only resolves repo + destination and hands over to
`setup-hermes.sh`, which owns the real install (pinned uv from `pm/lock.json`,
bootstrap Python, PM-managed environment). Env overrides:

| Var | Default | Effect |
|---|---|---|
| `HERMES_FORK_URL` | `https://github.com/VovaElkTver/hermes-autonomous-agent` | clone source |
| `HERMES_FORK_BRANCH` | `main` | branch to clone / fast-forward |
| `HERMES_HOME` | `$HOME/.hermes` | base dir; checkout goes to `<base>/hermes-agent` |
| `DRY_RUN=1` | off | print actions, touch nothing |

Installer decision tree: no `<dest>/.git` → clone; existing checkout with the
expected origin → `fetch` + `checkout` + `merge --ff-only` (a non-ff-able tree
fails — do not force); foreign origin → print found vs expected and exit 1
("refusing to touch it" is a protection, not a bug).

Agent procedure:
1. On an unfamiliar host smoke-test with `DRY_RUN=1` first; the printed plan
   must name the destination you intend.
2. Run the one-liner. Done when `setup-hermes` exits 0.
3. Activate: `source <dest>/activate` (bash; `activate.fish` for fish;
   `& <dest>\venv\Scripts\Activate.ps1` on Windows). Done when
   `hermes --version` answers.
4. Post-install checklist, in order: secrets into `~/.hermes/.env` (secrets
   only); settings via `hermes config set` (never hand-edit protected
   `config.yaml` with raw tools); gateway only if messaging surfaces are
   wanted: `hermes gateway install`, verify with `hermes gateway status`.
5. Verify: `git -C <dest> remote get-url origin` returns exactly the expected
   URL; `git -C <dest> log --oneline -1` matches `origin/$BRANCH` head.

Pitfalls: never silently repair a failed install (no improvised
`git reset --hard`, remote swaps, dependency pokes) — capture the log and
report to the operator. The PS1 path is not yet verified end-to-end on real
Windows: treat Windows failures as expected-unverified and report, don't
work around. Refusals mean the checkout's origin is neither the old
`.../hermes-agent` nor the current `.../hermes-autonomous-agent` URL (both
installers normalize the pre-rename URL): the fix is the operator's call —
`HERMES_FORK_URL=<URL>` or repoint the origin — never silent.

## Update

`hermes update` on the node: fast-forwards the checkout (`fetch` +
`merge --ff-only` onto `main`, auto-stash/restore of local changes) and runs
the maintenance tail: config migration with backup/rollback, bundled-skill
sync (content-diffed, convergent), state.db integrity guard, live plugin
catalog invalidation, runtime re-provisioning through PM.

Update forensics ("did it update? what did it do?"):

- `~/.hermes/logs/update.log` — step-by-step record of the last update
- `~/.hermes/logs/update_receipts/latest.json` — outcome receipt
- `git -C <dest> reflog --date=iso` — exact pull/merge timestamps
- `~/.hermes/state-snapshots/<ts>-pre-update` — pre-update state snapshot

The ZIP fallback (used when git is broken on a host, e.g. Windows) swaps the
source tree in two phases (stage → commit) and preserves only `venv`,
`.venv`, `node_modules`, `.git`, `.env` — user config, skills and state.db
live in `<HERMES_HOME>`, not in the tree, so they are never at risk either
way.

## Restart

- Shells inside a gateway session are gateway children: a restart typed in
  the session shell is blocked by the lifecycle guard and would kill the
  current turn mid-flight. The agent's default path is to **stage the change
  and hand the owner `hermes gateway restart`** — never restart in-session
  without an explicit restart order.
- With an explicit order: persist state first, then the D-Bus escape hatch
  (`busctl --user call ... Manager RestartUnit ... hermes-gateway.service`);
  the current turn dies at the bounce — that is expected, verify afterwards.
- Planning forks: a naked restart can amputate live forks. Use the
  drain-aware cycle `~/.hermes/scripts/gw_cycle_planning.sh [max_wait_s]`
  (default 600 s) — see the `fork-ops` skill for the full semantics
  (terminal fork events, boot-sweep reconciliation, ticker ownership).
- After any restart: `active (running)` ≠ working. Verify `MainPID` /
  `ExecMainStartTimestamp` actually moved, compare the last
  `~/.hermes/logs/gateway.log` timestamp to `date`, and probe the peer
  (homeserver) before blaming the gateway.

## Rollback

- Before the operation that may need it, the return SHA is in
  `DEPLOY-NOTE.md` (repo root, format in `operating.md` § Fleet).
- Rollback = `switch_release` (atomic re-link of `~/hermes-live`); zero git
  commands inside the emergency loop (invariant of
  `docs/design/safe-mode-rescue.md`).
- Post-boot health is verified by the survivor:
  `python -m hermes_rescue run-once --report`, then report to the owner room,
  then remove the note.

## Wedged boot

Gateway did not come up after a deploy: read `DEPLOY-NOTE.md` +
`docs/design/reanimation-protocol.md`, verify MainPID vs
`~/.hermes*/gateway.pid`, run `python -m hermes_rescue run-once --report`
(inspect → decide → kit → observe 120 s → report), report to the owner room,
remove the note only when the node is verified healthy.

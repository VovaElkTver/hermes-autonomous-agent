# docs/agents — operating reference for agents running this fork

Fork: `VovaElkTver/hermes-autonomous-agent`. This directory is the compact
operating reference for agents that run this fork (gateway nodes, forks,
scoped workers). It is NOT the codebase development guide — that is the
root `AGENTS.md` and its area files.

## Delivery

This directory lives in the repo checkout (`<HERMES_HOME>/hermes-agent`), so it
arrives on every **install** (the installer clones the tree) and every
**update** (`hermes update` fast-forwards the checkout; the ZIP fallback swaps
the source tree, only `venv`/`.git`/`.env`/`node_modules` are preserved).
Discoverability rides the `skills/autonomous-ai-agents/` skills
(install-fork-agent, controller-protocol), which sync into
`<HERMES_HOME>/skills/` on install and update.

## Read order

1. `operating.md` — the discipline: evidence, controller commands, time model,
   fleet rules, report model, remote writes. Read before acting on a live node.
2. `node-ops.md` — install / update / restart / rollback / wedged-boot
   protocol. Read before touching a node's lifecycle.
3. Deep dives: `docs/controller.md` (RU, full command table + `/for` grammar),
   `docs/design/reanimation-protocol.md`, `docs/design/safe-mode-rescue.md`,
   `docs/RELEASES.md` (live node pin — node-local state, not doctrine).

Keep this directory self-contained enough that a fresh node agent can operate
without the journal or the repo's development docs. When the code changes in a
way that changes a guarantee written here, update this directory in the same
commit — a stale guarantee is worse than none.

# operating.md — operating discipline of this fork

Condensed from the fork's operating rules (root `AGENTS.md` § "hermes autonomous
(elk)"), the reanimation protocol, and the controller contract. The long form
lives in the linked docs; this file is what an agent on a fresh node must hold
in mind before acting.

## Evidence

- **The tape is the only witness.** The only reliable record that a human sent
  something is the Matrix room history (client API) or the CLI session history.
  Hermes' own journals and databases are circumstantial. Verify ingress/loss
  claims against the tape before believing or refuting them.
- Internal durable records are evidence, not proof: they must agree with the
  tape; disagreement is an incident, not "a discrepancy in the records".
- "It ran without an error" is not a verdict. A result stands only if it is
  reproducible or has a visible consequence for the owner (test N/N, hash,
  path, tool run). A report starts with its limits.

## Acceptance belongs to the human

An agent proves that an action happened and was delivered; only the owner
decides "success". Successfully executed ≠ successful. When work is blocked,
say so concretely (what is missing, what permission is needed) instead of
stretching a plausible-looking result into a claimed one.

## Mechanisms live in one place

Any gate, filter or capture goes into the throat of the process — where the
action is born — never smeared across surfaces. Surfaces are transports.
Text instructions are not a reliability mechanism: if a failure repeats,
fix architecture/tools/state/feedback, not the prompt.

## Controller (managed autonomy)

Commands are recognized by CODE, before the agent's turn. An agent never
grants or revokes autonomy itself; its words are inputs to the router, its
verdicts are facts.

| Command | Examples | Effect |
|---|---|---|
| enable autonomy | «работай автономно», «приступай» | permission on, epoch+1; queue runs only in the contract's workdir |
| pause autonomy | «пауза», «замри, обсудим» | epoch+1; new autonomous dispatches block; running process gets interrupt_requested |
| resume autonomy | «продолжай автономно» | permission on, epoch+1 (stale approvals die) |
| cancel task | «отмени задачу task-xxxx» | task.state=cancelled; the run keeps its interrupt_requested separately |
| agree task | «иди task-xxxx» | MATERIAL agreed (agreed_at), draft→ready; pump dispatches when autonomy is on in scope |
| show status | «покажи статус задач» | task / contract_version / permission |

- `/for <A> in <set> [instruction]` forces a set through the controller queue
  (one draft task, one `ctl_step` per item, acceptance by trial per step);
  `--go` bakes the agreement into the command line.
- Pause does not forbid a separately requested interactive step, and does not
  resume the queue. A bare «го/иди» without a task id is not a command.
- Flag: `controller.enabled: true` in config.yaml (off by default). Check
  both `controller.enabled` and `plugins.disabled` before assuming a code bug:
  the contradiction is logged, and managed autonomy stays OFF.
- Full RU reference: `docs/controller.md`. Protocol behaviour:
  `controller-protocol` / `supervisor-protocol` skills.

## Time model

- The ticker is the fork's internal clock (1 Hz). Pump ticks, watchdogs and
  "in a minute" are tick counters, not wall time.
- **Deferred work is an event, not a timer.** Home is `hermes_events` (the
  wake seam: a due event becomes a session wake turn through the FIFO).
  Executing an event consumes it — single execution is the processor's own
  guarantee. Do not hang per-tick watchdog scripts over predicates.
- `cron` is banned as a scheduler of agent steps.

## Fleet: before touching a live gateway

- **Return-to-base.** An agent never schedules its own gateway restart from a
  live turn (`sleep N && restart` = orphan death — the turn dies with the
  gateway, no witness survives). The death executor is external: a detached
  reboot script, `/reset-safe`, the owner's hand, or rescue.
- **Deploy = commit on a fleet branch; rollback = `switch_release`** (atomic
  re-link of `~/hermes-live`). Zero git commands inside the emergency loop.
- **Post-boot health is verified by the survivor**, not by a dead agent's
  promise: `python -m hermes_rescue run-once --report` (inspect → decide → kit
  → observe → report).
- **Wedged boot / triumph over a corpse.** A fresh session waking on a
  suspicious node: read `DEPLOY-NOTE.md` (repo root) +
  `docs/design/reanimation-protocol.md`, verify the unit's MainPID against
  `~/.hermes*/gateway.pid`, run rescue, report to the owner room, then remove
  the note. A healthy gateway deletes the note in the commit that closes the
  deploy.

DEPLOY-NOTE.md (repo root; must not outlive the deploy):

```
deploy: <what and why, one line>
from:   <SHA to return to>
to:     <target SHA/branch>
risk:   <what may fail to boot and why>
who:    <agent/branch that left the note>
```

Working tree discipline for live nodes: everything committed or
`git stash push -m "<what/why>"`. An undeclared local diff on a live runtime
is banned — precedent: a `start_writer` diff without `thread.start()`
silently disabled housekeeping on production, and nobody knew but the diff.

## Report model (fork branches)

A branch REPORTS ("I consider saving to X necessary") — it never asks
permission and never holds a pending question as state. Execution happens
only on a human decision (record / forget / rephrase), by the owner or a
human at the CLI. The right to write never travels into the branch.

## ToDo and stages

`ToDo.md` at the repo root: work proceeds in stages named `stageNNN` in
order; a defect found mid-stage is appended to ToDo.md and reorders measures
as needed.

## Remote writes

All git writes (push, PR, merge) go only to `VovaElkTver/*` (origin).
`NousResearch/hermes-agent` (upstream) is read-only for this fork: fetch yes,
push/PR never.

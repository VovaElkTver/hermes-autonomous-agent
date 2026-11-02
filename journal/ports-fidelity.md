# Ports fidelity: donor @6337fc8 vs our ports

Audit: 2026-11-02. Donor: `deepseek-ai--dsh-superagent-prototype` @6337fc8 (TypeScript).
Our ports: `/home/qwen/hermes-agent/controller/` (Python).
Scope: five mechanism ports. No code changes — research only.

---

## 1. effects

| | Donor | Ours |
|---|---|---|
| Path | `src/app/effects.ts` | `controller/effects.py` |
| Concurrency | async; `queueMicrotask` dispatch; `Promise.allSettled` for idle/drain | synchronous pull-based; `run_pending()` called by pump/ticker |
| Start gate | `started` flag; effects only run between `start()` and `drain()` | no start gate; `run_pending()` always callable |
| `kick()` | schedules a microtask dispatch | no-op (back-compat stub) |
| `idle(kind?)` | resolves when no effects of that kind running | absent (replaced by `drain()` for tests) |
| Un-ordered batch fetch | `LIMIT 16 + running.size` | `LIMIT 32` (fixed) |
| Concurrency limit | `room = handler.limit() - running.size` (tracks live tasks) | `done >= max(1, limit())` (tracks per-pass completions) |
| Ordered kinds | block behind oldest row even when not yet due; `limit()` queried only after ready rows exist | `LIMIT 1` + `break` on first failure (single-flight) |
| Health reporting | `HealthBoard.claim` + `HealthEntry.set` (external board) | `status(kind)` returns a dict; no external board |
| `broken` set | tracks kinds whose last dispatch failed; next success refreshes | absent; `status.failing` derived from `attempts >= 3` |

Preserved semantics: upsert coalesces on (kind,key) keeping seq; generation bump prevents stale delete; `Later` reschedules without incrementing attempts; exponential backoff `min(max, initial * 2^min(attempts,16))`; `FAILURES_BEFORE_ERROR = 3`; rows persist across restarts.

Semantic divergences:
- No `idle()` equivalent: callers that need "wait until all of kind X finish" must poll `run_pending` to exhaustion (our `drain` is test-only, not a production await).
- Concurrency accounting is per-pass, not per-live-task: two concurrent `run_pending` calls (if the pump ever did that) would both see `done=0` and could exceed `limit`.
- The donor's `start/drain` lifecycle (effects hold while processing is paused) has no counterpart; our effects dispatch unconditionally.

---

## 2. outbox

| | Donor | Ours |
|---|---|---|
| Path | `src/app/outbox.ts` + `store.ts` (receipts) | `controller/outbox.py` |
| Effect kind | `'reply'`, retry [5000, 300000] ms | `'outbox_reply'`, retry (5.0, 300.0) s |
| Source validation | checks `sources.instance(source).enabled` → `Later`; `channelAllowed` → settle `channelIgnored` | absent; `deliver` callable expected to handle or raise |
| 'waiting' result | `Later` (source not installed) | absent; only `'sent'` and `'gone'` accepted |
| Drop reasons | `channelIgnored`, `channelGone` | `conversationGone` (single reason) |
| `sent()` | returns `effects.idle('reply')` (Promise) | absent |
| `behind` | joins on generated column `pending = ticketId \|\| ' ' \|\| channel.thread` | joins on `conv_key` + `task_id` (NULL-aware) |
| Settle recreate-on-race | UPDATE miss → INSERT with settled state | same (UPDATE miss → INSERT OR IGNORE) |
| `withdraw` | `store.atomic(() => { if receipts.withdraw(id) then effects.cancel })` | DELETE WHERE status='pending' + effects.cancel if rowcount |

Preserved semantics: idempotent msg_id (INSERT OR IGNORE / ON CONFLICT); per-conversation ordering via `behind` gate; pending→sent|dropped state machine; dropped receipts retained permanently; settle-recreate guard for withdrawn-mid-send.

Semantic divergences:
- No source-availability check in the delivery handler: if the transport is down, the `deliver` callable must raise a generic exception (counted as a failure) rather than `Later` (zero-cost reschedule). This means a down transport burns failure attempts and eventually trips `FAILURES_BEFORE_ERROR` where the donor would wait indefinitely via `Later`.
- No `channelIgnored` path: a conversation that the source no longer listens to has no distinct settle path; it must surface as an exception from `deliver`.
- Single drop reason (`conversationGone`) vs two donor reasons; the "source stopped listening" case is unrepresented.

---

## 3. mandate

| | Donor | Ours |
|---|---|---|
| Path | `src/core/model.ts` (Mandate) + `src/policy.ts` (revalidate, policyVersion) | `controller/mandate.py` |
| Mandate fields | `strength`, `policyVersion`, `basis` (discriminated union, 5 variants), `actor: {source,id}`, `scope` | `strength`, `policy_version`, `basis` (flat string), `actor` (string), `scope` |
| POLICY_REVISION | `'mandate-v3'` | `'mandate-v1'` |
| policyVersion input | `JSON.stringify({ rules, mode, sources })` — assessment rules, eval mode, source policies | `sha256(json.dumps({rev, perm}))[:16]` — only `ctl_autonomy_permission` rows (scope, enabled) |
| Revalidate | per-basis-type logic: checks source still listened, rules still enabled, mode still auto, sender still listed, bot still on list, admission cell still grants strength | `policy_version == compute_policy_version()` AND `withdrawn_at IS NULL` |
| Withdraw | implicit: a policy change revokes (revalidate returns false) | explicit: `withdraw()` sets `withdrawn_at`; only a human stop calls it |

Preserved semantics: mandate is immutable once issued; wider scope → new ticket (enforced by caller); a ticket's end does not end its mandate; revalidation at resume; only a human stop withdraws.

Semantic divergences:
- Policy version scope: the donor hashes the full assessment-rule set + eval mode + source policies (rules, allowed lists, admission tables, bot lists). Ours hashes only autonomy-permission rows. Changes to rules, eval mode, or source allowed-lists do NOT invalidate our mandates but DO invalidate the donor's.
- Revalidate granularity: the donor checks five `via` variants with different rules (e.g., a `bot` mandate fails if the bot is removed from the list; an `admission` mandate fails if the sender's cell drops below the original strength). Ours has a single binary check: version match or not.
- `MandateBasis` structure: the donor's discriminated union (addressed/followup/admission/listed/bot) carries `sender`, `rules`, `cell`, `observation.mode` etc. for audit and revalidation. Ours stores a free-form `basis` string.

---

## 4. disposition

| | Donor | Ours |
|---|---|---|
| Path | `src/core/model.ts` (TriggerDisposition + followupOwners) + `src/core/intake.ts` | `controller/disposition.py` |
| Stored fields | `filtered?`, `evalId?`, `decisionId?`, `fanout`, `context?`, `requeued?`, `handbacks?: {ticketId, reason, at}[]`, `acked?`, `at` | `fanout`, `context`, `handbacks`, `acked`, `requeued` (int), `note`, `at` |
| handback items | `{ticketId, reason, at}` | `{task_id, reason}` (no per-item timestamp) |
| `requeued` type | `true \| undefined` | integer 0/1 |

`followupOwners` comparison:

| Aspect | Donor | Ours |
|---|---|---|
| Human check | `trigger.human` | `human: bool` param |
| Requeued check | `disposition.requeued` | same |
| Returned/context filter | `handbacks.ticketId` + `context` array | same |
| Thread-evaluation check | if the trigger's thread has an `evaluating` that includes the trigger → no owners | absent |
| Ticket liveness | `liveTicket(ticket)` OR (ended not-by-human AND has undelivered input for this trigger) | pre-computed `live_task_ids` parameter |
| Undelivered-input check | `ticket.undelivered.includes(input.id)` for ended tickets | absent |

Semantic divergences:
- The donor's `followupOwners` reads live ticket/thread state to determine whether an ended ticket can still own a follow-up (via undelivered inputs). Ours delegates this to the caller via `live_task_ids`; the disposition module itself cannot answer "can this ended task still decide?"
- The donor checks whether an active Eval on the trigger's thread is holding it (in which case no ticket owns it, Eval will decide). Ours has no equivalent — the caller must not call `followup_owners` while an evaluation is in progress.
- Donor's `filtered` / `evalId` / `decisionId` fields record upstream routing. Ours replaces them with a free-form `note` string.

---

## 5. receipts

| | Donor | Ours |
|---|---|---|
| Path | `src/core/assignments.ts` + `src/core/tickets.ts` + `src/core/model.ts` | `controller/receipts.py` |
| Storage | fields on in-memory objects: `Assignment.deliveredAt`, `Assignment.idleAt`, `TicketInput.readAt`, `TicketInput.deliveredTo` | dedicated table `ctl_input_receipt(run_id, event_id, delivered_at, read_at)` |
| `delivered` | sets `input.readAt = null`, `input.deliveredTo = assignmentId`, removes from `ticket.undelivered`, updates `assignment.deliveredAt` | INSERT OR IGNORE receipt row with `delivered_at` |
| `read` | `input.readAt \|\|= now()` (first wins) | `UPDATE SET read_at WHERE read_at IS NULL` (first wins) |
| `interrupted` predicate | `deliveredAt >= report.at` → busy → restart; else settled | `last_delivered_at >= last_report_at` (None→True) |
| `requeueUntaken` | filters: `readAt === null && deliveredTo === replaced.id && !undelivered.includes(id)`; unshifts to `ticket.undelivered` | filters: all unread events for old run; INSERT OR IGNORE into new run's receipts; DELETE from old run regardless |
| `idleAt` / `turnEnded` | tracked on assignment; feeds into `readUntil` | absent |
| `readBefore` / `readUntil` | complex: compares input's effective read time (deliveredAt or readAt) against report.at or idleAt, whichever is later | absent; only `read_at IS NULL` check |

RECOVERY_NOTE:
- Donor: "…Inspect the current worktree, git history **and GitHub state** before continuing…"
- Ours: "…Inspect the current worktree, git history **and external state** before continuing…"
- Semantic intent identical (audit before acting, don't repeat completed operations); wording generalized from GitHub-specific to platform-neutral.

Semantic divergences:
- `requeue_untaken` scope: the donor only requeues inputs whose `deliveredTo` matches the replaced assignment. Ours requeues ALL unread inputs of the old run, including those delivered by earlier assignments that were never read. In practice this is equivalent if each run is 1:1 with an assignment, but it differs if a single run delivered inputs on behalf of multiple assignments.
- No `idleAt` / `turnEnded` tracking: the donor uses `idleAt` to compute `readUntil` (the deadline by which an input must have been read). Ours has no equivalent concept; an input is "unread" until `read_at` is set, with no deadline.
- No `deliveredTo` tracking: the donor records which assignment each input was delivered to; this enables the precise `requeueUntaken` filter and the `readBefore` logic. Ours has no per-input assignment association.

---

## Summary

All five ports preserve the donor's core invariants (durability, idempotency, backoff, coalescing, first-read-wins, recreate-on-race, Later-reschedule). The main simplifications are:

1. **Synchronous vs async**: no task tracking, no `idle` await, no start/drain lifecycle gate.
2. **Flattened policy version**: hashes permission rows only, not rules/modes/source-policies.
3. **No per-basis revalidation**: single version-match instead of five-variant logic.
4. **Externalized liveness**: `followup_owners` and `requeue_untaken` take pre-computed inputs rather than reading ticket/thread state internally.
5. **Reduced delivery state**: no source-availability `Later`, no `channelIgnored`, no `idleAt`/`turnEnded`.

No port introduces a semantic that contradicts the donor; all divergences are omissions or generalizations, not inversions.

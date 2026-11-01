# Investigation mechanism — implementation spec (fork anchors)

Status: INSTALLED (01.11). Skeleton merged `3ca912a96f`; all hooks cut into the
runtime per §8 order — H2+R1 `5373052628`, H1 `6e80dc11aa`, H4 `913ecab3fe`,
fork-stamp+H3+boot-settle `692b1bc621`. Each hook carries one
``INVESTIGATION-HOOK:`` marked call site; logic lives in
``agent/investigation_hooks.py`` (fail-open). Tests: tests/test_investigation_hooks.py.
Deploy: effective after a cold gateway restart; remaining = live behavioral run.
Doctrine (Вл,
ToDo 01.11): the core stores conditions only — provenance, delivery dedup,
restart recovery; hypothesis choice / experiment choice / verdict belong to the
agent, no external critic, no cron, no human arbitration point. State = rows in
the session DB, modelled on `session_branches`. The feed (Matrix API / CLI
history) is the only reliable evidence of execution; DB rows are circumstantial.

All `path:line` cites verified on this tree (branch `feat/investigation`, base
df317af727) at writing time. Lines drift; re-verify before cutting.

## 1. Stored state

Five tables, DDL appended to `SCHEMA_SQL` immediately after the
`session_branches` block — `hermes_state_common.py:775-853` (SCHEMA_SQL opens at
`hermes_state_common.py:451`). Shape mirrors `session_branches`
(`hermes_state_common.py:754-774`): one parent row + child rows + partial unique
index, all `IF NOT EXISTS`.

| Table | Row | Key fields |
|---|---|---|
| `investigations` | one question a session is working | `session_id`, `question`, `status open→closed`, `revision`, `summary`/`constraints` (close), `meta_json` (close diagnostic) |
| `investigation_hypotheses` | one hypothesis version | `statement`, `status` (model's word via assess, verbatim; default `standing`) |
| `investigation_probes` | one planned check | `description`, `status pending→dispatched→observed\|unknown_outcome`, `branch_id` (structural /fork link) |
| `investigation_predictions` | one expectation (probe × hypothesis) | `expected`, `created_at` — the BEFORE-act record; pair UNIQUE |
| `investigation_observations` | one captured candidate | `source tool\|branch`, `dedup_key` UNIQUE, `assessed 0/1`, `verdict` (model's word), `meta_json` (`no_predictions` diagnostic) |

Constraints the mechanism owns (each pinned by a test, §5):

* **One open investigation per session** — partial unique index
  `idx_investigations_open` (`hermes_state_common.py:797-798`);
  `open_investigation` returns the live row on replay instead of forking a
  second one (`hermes_state_investigations.py:67-91`).
* **Predictions precede the probe** — `add_prediction` refuses once the probe
  has an observation (`hermes_state_investigations.py:248-258`); a capture for a
  probe with zero predictions is stored but flagged
  `meta_json.no_predictions` (`hermes_state_investigations.py:295-298`).
* **Only `assess` accounts an observation** — runtime capture sets `assessed=0`;
  `assess_observation` is the single writer of `assessed=1`
  (`hermes_state_investigations.py:338-365`).
* **Close is optional, dropping evidence is not** — `close_investigation`
  refuses while `assessed=0` rows exist, records the refusal as a
  `meta_json.close_diagnostic`, and leaves status `open`
  (`hermes_state_investigations.py:367-406`).

### SCHEMA_VERSION

No bump. The repo rule is explicit: the bump "only records that the declared
shape changed; a new table or column needs no bump"
(`hermes_state_common.py:284-289`, current value `SCHEMA_VERSION = 32` at
`:289`). Drift is caught by the read probe derived from `SCHEMA_SQL`
(`hermes_state_schema.py:169-177`) and healed by the declarative
`executescript(SCHEMA_SQL)` + `_reconcile_columns()` that every writable open
already runs (`hermes_state_schema.py:772-775`, `:956`). The new tables are
therefore created on first writable open of any existing store; a read-only
open against a stale store raises at prepare time (probe contract) exactly as
for any other unit. A hypothetical future bump would be 33.

### Mixin

`hermes_state_investigations.py` — `class InvestigationMixin` (:36), same
pattern as `SessionBranchMixin` (`hermes_state_branches.py:20`): writes through
`_execute_write` / `_write_rowcount` with
`patience_s=self._TRANSCRIPT_WRITE_PATIENCE_S`
(`hermes_state.py:1011-1018`, `:1138`, constant `:489`), reads through
`_read_one` / `_read_all` (`hermes_state.py:1148-1152`); every write callback is
idempotent under the whole-callback retry contract
(`hermes_state.py:1014-1018`). Mixed into `SessionDB` beside
`SessionBranchMixin` (`hermes_state.py:73`, `:465-471`) — additive class-list
edit, no behaviour change for sessions that never use it. `AsyncSessionDB`
(`hermes_state.py:1722-1734`) exposes every method to the gateway
automatically via `__getattr__` offload.

Id helpers: `new_investigation_id/new_hypothesis_id/new_probe_id`
(`hermes_state_investigations.py:42-64`) mirror `new_branch_id`
(`hermes_state_branches.py:65-67`).

## 2. Model tool `investigation`

`tools/investigation_tool.py`. Schema `INVESTIGATION_SCHEMA` (:27-98) in the shape
of `FORK_SCHEMA` (`tools/session_fork_tool.py:14-28`); single tool, `op` enum
dispatch — four operations:

* `open {question, hypotheses[], probes[{description, predictions[{hypothesis,
  expected}]}]}` → `investigation_id`, `hypothesis_ids[]`, `probes[]`;
  `_op_open` (:119-152). Replay returns `already_open` + live id, no second row.
* `predict {probe_id, predictions[{hypothesis_id, expected}]}` → per-pair
  `created` flag; refuses after the fact (:154-177).
* `assess {observation_id, verdict, hypothesis_updates[]}` → the only path to
  `assessed=1`; the core stores the model's words verbatim (:179-194).
* `close {summary, constraints}` → `{closed, unassessed}`; refused close keeps
  the investigation open with the diagnostic (:196-216).

Registration is the `session_fork` path: the module self-registers on import via
`registry.register(name="investigation", toolset="sessions", …)`
(`tools/investigation_tool.py:230-241`, pattern at
`tools/session_fork_tool.py:116-134`). No `toolsets.py` edit is needed:
`"sessions"` is a registry-born toolset (`tools/fork_explore_tool.py:656-663` is
the third rider), merged in by `get_all_toolsets()`
(`toolsets.py:430-444`) / `get_toolset(..., include_registry=True)`
(`toolsets.py:274-303`) and picked up by `_select_tool_names` when no explicit
toolset list is configured (`model_tools.py:313-330`). `discover_builtin_tools`
imports every self-registering `tools/*.py`
(`tools/registry.py:112-121`, `:94-110`), so `tools/investigation_tool.py` is
picked up with zero further wiring.

`handler=lambda args, **kw: investigation(args, db=kw.get("db"),
current_session_id=kw.get("current_session_id") or kw.get("session_id") or "")`:
`_kwargs_accepted_by` signature-inspects and passes the dispatcher kwargs
(`tools/registry.py:936-944`, `:1068-1078`); registry dispatch injects
`session_id` (`model_tools.py:816-832`), the inline path injects
`current_session_id` (`agent/inline_tool_executors.py:123-139`). See §6 R1 for
the `db` gap — same shape `session_fork` already has.

## 3. Loop hooks (marked points — no runtime edits in this change)

The turn loop is `agent/conversation_loop.py:1714-1758`: per iteration
`begin_iteration → prepare_iteration → assemble_api_request → preflight →`
API retry loop `→ run_tool_round | finish_text_response`
(:1717-1752; phase split at :1750-1752). Each hook below is a one-line call plus
an `INVESTIGATION-HOOK:` comment; all four read state through the mixin only.

### H1 — render active investigation before the model request

`INVESTIGATION-HOOK: H1 render` — insert in `agent/conversation_loop.py`
between :1719 (`prepare_iteration`) and :1720 (`assemble_api_request`), or
inside `assemble_api_request` right after `build_api_messages`
(`agent/turn_request_assembly.py:106-127`), before the ordering-sensitive
normalization block documented at :111-113. Insertion body:

```python
note = agent._session_db and agent._session_db.render_active_investigation(agent.session_id)
if note:
    messages.append({"role": "user", "content": note, "_investigation_note": True})
```

`render_active_investigation` is pure formatting of stored rows
(`hermes_state_investigations.py:449-…`). Cost accounting: appending to the
tail preserves the cached prefix (per-conversation prompt caching is sacred);
do NOT inject mid-history. The row is ephemeral — flag it and pop it like the
existing scaffolding flags (`agent/turn_final_response.py:331-338` pop loop over
`_EPHEMERAL_SCAFFOLDING_FLAGS`, defined :31), so it never becomes durable
transcript; alternatively stamp it on the API copy only inside
`assemble_api_request` (which already receives `messages`
`agent/conversation_loop.py:1720`). One note per model request is fine; the
render is idempotent between requests if state did not change (byte-stable
under unchanged revision — `revision` only moves on real state change,
`_bump_revision` `hermes_state_investigations.py:106-109`).

### H2 — capture_tool_result after a normal tool execution

`INVESTIGATION-HOOK: H2 capture` — insert in `agent/tool_executor.py` right
after the tool-result append + durable flush, i.e. after
`messages.append(tool_message)` (:1173) and the
`_flush_session_db_after_tool_progress` success (:1174-1175). At that point the
full identity is in scope: `function_name`, `function_args`, `tool_call_id`,
`persisted_result` (built into the row at :1156 by
`make_tool_result_message`, `agent/tool_dispatch_helpers.py:448-480`, whose
`tool_call_id` is the canonical call-id half, :441-445). Insertion body:

```python
capture_tool_result(agent, function_name, function_args, tool_call_id, tool_message)
```

Semantics (mixin `capture_observation`
`hermes_state_investigations.py:278-313`): no-op unless the session has an open
investigation AND the call binds a probe — binding rule: `tool_call_id` of a
tool call the model issued naming `probe_id` in its args (for `terminal`/
`execute_code`/`session_fork`: args carry `probe_id` only if the model echoed
it, which the H1 render offers verbatim); otherwise capture unbound
(`probe_id=None`). Skip the `investigation` tool's own calls (recursion
noise). Dedup by `dedup_key` (§4), so re-delivery — the same tool executed
twice, or a replay from resume — yields one observation.

### H3 — capture_branch_result after a join

`INVESTIGATION-HOOK: H3 join` — insert in `join_branch`
(`gateway/slash_commands_fork.py:183-204`) after
`set_branch_status(... "joined")` (:200), before the `_evict_cached_agent`
block. `join_branch` is shared by `/join` (:181) and the agent tools; the
`note` strategy's work-note is already in `messages` at :195-198 and the `note`
text is the observation content. Insertion body:

```python
await capture_branch_result(self._session_db, branch, strategy, note_or_none)
```

Provenance is structural, never an embedding: when the branch was created for a
probe, `/fork` records `investigation_id` / `probe_id` / `purpose` in the
branch's own `session_branches.meta_json` (merge helper `set_branch_meta`,
`hermes_state_investigations.py:215-239`, same merge shape as
`set_branch_title` `hermes_state_branches.py:47-63`), and the probe stores the
`branch_id` back (`bind_probe_branch` :207-213). At join, the handler reads
`branch["meta_json"]` (the row dict is already in hand at
`gateway/slash_commands_fork.py:188-189`; `get_branch` returns `meta_json`,
`hermes_state_branches.py:90-96`) and captures with
`source='branch'`, `dedup_key = observation_dedup_key_branch(parent, branch_id)`
(`hermes_state_investigations.py:52-56`). Branches without the meta are ordinary
forks — hook no-ops (addivity: sessions without investigations never touch the
tables). Multiple branches agreeing does not upgrade a hypothesis: they arrive
as separate observations and each still needs its own `assess`; no counter
exists in the schema.

The `note`-fallback in the agent-facing `session_join` tool
(`tools/session_fork_tool.py:105-113`, `set_branch_status` at :111) reaches the
same capture only if routed through `join_branch`; until that routing lands,
H2's terminal-call capture already records the branch result inside the branch
session — the hook adds parent-side attribution.

`/fork` side (`_fork_impl` `gateway/slash_commands_fork.py:75-140`): after
`create_branch_row` (:122-125) and `set_session_title` (:126-127), stamp
`set_branch_meta(branch_id, {investigation_id, probe_id, purpose})` when the
fork was issued for a probe (the model states it in the fork name / a follow-up
`predict`; mechanism stores what it is told, decides nothing).

### H4 — one-time reminder before a tool-call-less turn ends

`INVESTIGATION-HOOK: H4 reminder` — insert in `apply_stop_gates`
(`agent/turn_stop_gates.py:134-223`), after the kanban gate (:202-218) and
before the terminal `return StopGateVerdict(continue_turn=False, …)` (:219-223),
as a fifth gate in the existing ladder (the ladder is exactly this shape:
verify-on-stop → pre_verify → kanban, :139-142). Gate body: if the session has
an open investigation with `unassessed_count(...) > 0` and the per-turn counter
`getattr(agent, "_investigation_stop_nudges", 0) < 1`, use the existing
`_continue(nudge, "_investigation_stop_synthetic")` helper (:144-159) with text
listing the observation ids and increment the counter. **ONE** nudge per turn:
the counter pattern is the established one-time mechanism
(`_verification_stop_nudges` :180, `_kanban_stop_nudges` :204) — a second
tool-call-less arrival passes the gate and the turn ends normally: the answer to
the human is NOT held hostage (close is optional), the investigation stays open
with `close_diagnostic`/unassessed rows as the visible diagnosis, and the next
H1 render shows it. `apply_stop_gates` already runs on exactly the
no-tool-calls exit (`agent/turn_final_response.py:340-347` called from
`finish_text_response`, `agent/turn_final_response.py:71`), so no loop surgery.

## 4. Delivery dedup (first-class invariant)

Key: `investigations.dedup` is enforced by
`CREATE UNIQUE INDEX idx_investigation_observations_dedup`
(`hermes_state_common.py:850-851`) over the value built by
`observation_dedup_key_call/observation_dedup_key_branch`
(`hermes_state_investigations.py:44-56`):

* tool capture: `<session_id>|call:<tool_call_id>` — `tool_call_id` is the
  provider call id normalized at row construction
  (`agent/tool_dispatch_helpers.py:441-445`), stable across retries and replay;
* branch capture: `<session_id>|branch:<branch_id>` — one join, one row.

Check site: the single `INSERT OR IGNORE` in `capture_observation`
(:300-308) — a duplicate delivery gets `rowcount 0` → the function returns
`None` and the probe is NOT flipped to `observed` again (:309-312), so the
re-delivery neither duplicates the observation nor re-fires downstream state.
Session-scoping the key (rather than a bare global UNIQUE) keeps provider
call-id collisions across sessions from silently eating evidence
(test `test_same_call_id_in_other_session_is_not_deduped`). The
`investigations` open-row dedup (§1) is the same pattern: a replayed `open`
delivery returns the live investigation (`hermes_state_investigations.py:75-78`);
predictions dedup by the UNIQUE `(probe_id, hypothesis_id)` pair
(`hermes_state_common.py:831-832`, check in
`hermes_state_investigations.py:252-258`).

## 5. Restart recovery

At gateway boot, `settle_unresolved_probes()` runs BEFORE
`_schedule_resume_pending_sessions()` (`gateway/run_startup.py:1593`; the call
site belongs beside the other pre-resume passes in the same block, :1575-1594 —
the scheduler is the existing auto-resume the doctrine points at, its
freshness-window doc :620-624). It flips `dispatched` probes with no
observation to `unknown_outcome` (`hermes_state_investigations.py:422-447`):
the outcome's source after a crash is unknown, so the mechanism records that
and NEVER auto-re-runs — re-running is the model's decision, made against the
H1 render that now shows `unknown_outcome`.

Waking the owner reuses the existing `resume_pending` machinery unchanged:
interrupted sessions are already marked by
`_mark_running_sessions_resume_pending` on shutdown
(`gateway/run_shutdown.py:872-884`) and auto-continued at boot by
`_schedule_resume_pending_sessions` (`gateway/run_startup.py:620-676`, called
:1593), whose empty-text internal event carries the reason-aware note
(`gateway/run.py:1083` `_prepare_resume_pending_message`). For a session that
died with an OPEN investigation but no interrupted turn, the same
`mark_resume_pending(session_key, reason=…)` is the wake primitive
(`gateway/session_lifecycle.py:263-274`) — the H1 render then re-presents the
investigation on the resumed turn; no new timer, no cron (ToDo: deferred work
is an event, and here the event IS the existing boot auto-resume). A
`session_turn_leases` row (`hermes_state_common.py:711-716`) tells a mid-turn
death from an idle one; the settle + wake pair covers both without inventing a
watchdog.

## 6. Risks / open points

* **R1 — `db` kwarg not in registry dispatch.** `model_tools._execute_tool`
  injects only `task_id`/`session_id`/`user_task`/`enabled_tools`
  (`model_tools.py:816-826`), and `_kwargs_accepted_by` narrows to the handler
  signature (`tools/registry.py:1068-1078`), so `kw.get("db")` is `None` on the
  registry path and the tool answers `session database unavailable`. This is
  the EXACT shape `session_fork`/`session_join` already have
  (`tools/session_fork_tool.py:31-36`, `:75-80` — their only working callers
  pass `db=` explicitly, tests/test_session_fork_branches.py::TestAgentTools).
  Fix at cut time: dispatch `investigation` inline (add an
  `INLINE_TOOL_EXECUTORS` entry beside `_session_search`, which injects
  `db=session_db, current_session_id=agent.session_id` —
  `agent/inline_tool_executors.py:123-139`, table :253-312, dispatch
  `agent/tool_executor.py:1690-1694`). UNVERIFIED whether
  `session_fork` is wired that way anywhere else — grep found no other
  dispatcher (`registry.dispatch` single callsite `model_tools.py:832`).
* **R2 — H1 mutation vs prompt cache.** Appending the render as a durable
  transcript row every iteration rewrites the tail every turn; keep it
  ephemeral (pop by flag, §3 H1) or API-copy-only, or the mechanism multiplies
  cost — the sacred-prefix invariant. If both H1 and a compression run in the
  same iteration, `compress_after_tool_results`
  (`agent/turn_tool_round.py:210-226`) may drop the ephemeral note with the
  rest of the tail; acceptable, the next request re-renders.
* **R3 — untrusted content.** Captured tool results feed a stored observation
  that the model re-reads via H1/assess; keep the raw text as already wrapped
  by `make_tool_result_message` (`agent/tool_dispatch_helpers.py:455-463`
  `_maybe_wrap_untrusted`) rather than re-unwrapping — the H2 hook should
  capture `tool_message["content"]` verbatim.
* **R4 — size.** Observation bodies cap at `OBSERVATION_MAX_CHARS` (8000,
  mirrors `JOIN_NOTE_MAX_CHARS` `gateway/slash_commands_fork.py:29`) with an
  explicit truncation marker (`hermes_state_investigations.py:28-31`,
  `:291-293`); the render lists ids + heads, not bodies.
* **R5 — mixed-invalid-batch calls.** The error-result rows appended inside
  `run_tool_round` (`agent/turn_tool_round.py:116-125`) never reach the
  `_execute_tool_calls` path, so H2 (placed after :1173 in tool_executor.py)
  misses them by design: an error stub is not a real observation.
* **R6 — revision is a diagnostic, not a lock.** `revision` bumps on every
  state-changing write (`_bump_revision`); there is no optimistic-concurrency
  check yet (the mechanism has exactly one writer per session turn by the
  gateway turn-lease design, `hermes_state_common.py:711-716`).
* **R7 — close-diagnostic accumulation.** A refused close overwrites the single
  `meta_json.close_diagnostic`; that is deliberate (latest refusal wins), but it
  means repeated closes don't count attempts. If the fleet wants a counter,
  append — don't re-shape the table.

## 7. Test surface (all green, no gateway, tmp DB)

`tests/test_investigations.py` — 25 tests: DDL boots (`TestSchemaBoots`), open
idempotence + one-open-per-session (`TestOpen`), predict-before-observation +
pair idempotence + missing-prediction flag (`TestPredictBeforeObservation`),
duplicate tool-call / duplicate branch-join / cross-session non-dedup
(`TestDeliveryDedup`), assess-only accounting (`TestAssessAccounting`),
close-without-assess stays open with diagnostic + closes after assess + edits
refused when closed (`TestCloseDiscipline`), restart settle + session scope
(`TestRestartSettle`), fork binding round-trip (`TestForkBinding`), tool
contract: registration toolset, four ops, end-to-end ops, refused close,
after-the-fact predict (`TestToolContract`). Pattern follows
`tests/test_session_fork_branches.py:11-18` (tmp `SessionDB` fixture); the
`AsyncSessionDB` facade check follows
`tests/gateway/test_stage001_p5_grid_fallout.py:62`.

## 8. Cut order (when runtime edits are authorized)

1. H2 (capture) alone — records exist, nothing reads them; zero blast radius.
2. H1 (render) — model can finally see state; watch prompt-cache cost (R2).
3. H4 (reminder) — closes the loop; one nudge per turn.
4. `/fork` stamp + H3 join capture — branch results become evidence.
5. Boot `settle_unresolved_probes()` + `resume_pending` wake.
Each step ships behind the existing turn tests; none of the four hooks edits
another cohort's file beyond the marked one-liner.

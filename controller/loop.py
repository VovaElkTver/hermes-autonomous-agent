"""controller.loop — the autonomous cycle (spec §20), one worker, one scope.

Every iteration is a full deterministic pass:
  permission check (fresh epoch read) -> pick task -> pick step -> barrier admit
  -> run step via worker_fn -> on finish: artifact + checkpoint review -> advance.

worker_fn(run, task, step) -> {"status": "finished"|"failed"|"interrupted",
                               "artifacts": [..payloads..]}
review_fn(run, step, artifacts) -> {"verdict": "pass"|"revise"|"needs_human", "detail"}
The loop NEVER trusts worker 'done' without review (acceptance row §46) and stops on
pause/permission loss between iterations and at dispatch (race row §7/§20).
"""
from __future__ import annotations

import hashlib
import json
import time

from . import barrier, model, queue as Q, transitions


def _artifact_hash(payloads: list) -> str:
    return hashlib.sha256(json.dumps(payloads, sort_keys=True,
                                     ensure_ascii=False).encode()).hexdigest()


def _park_task_paused(conn, task_id: str, *, basis: str) -> None:
    try:
        transitions.transition(conn, task_id, "paused", basis=basis)
    except ValueError:
        pass  # already paused/closed under another writer — the step state is the truth


def _resolve_worker_outcome(conn, *, task: dict, step: dict, run_id: str, res: dict):
    """Classify a finished worker against the DB, right after the turn ended.

    Preemption truth is the step READ-BACK, never the worker's exit status: a turn cut
    short can still report "finished" (or die reporting "failed") while the scheduler
    already flipped the step to paused underneath it. Returns ``None`` to proceed to
    artifact/review on a clean finish, else ``{"entry": <log line>, "stop": bool}``.
    """
    sid, tid = step["step_id"], task["task_id"]
    cur = model.get_step(conn, sid)
    if cur is not None and cur["status"] == "paused":
        model.set_run_status(conn, run_id, "interrupted")
        _park_task_paused(conn, tid, basis="out-of-band preempt")
        return {"entry": {"task": tid, "run": run_id, "step": sid,
                          "to": "paused", "preempted": True}, "stop": True}
    status = res.get("status", "failed")
    model.set_run_status(conn, run_id, "finished" if status == "finished" else status)
    if status == "interrupted":
        # Worker cut short with no external pause recorded: park the step paused with
        # the worker's own handoff so a resume re-anchors instead of restarting blind.
        model.pause_step(conn, sid, task_id=tid,
                         note=str(res.get("handoff") or res.get("error")
                                  or "worker reported interrupted"))
        _park_task_paused(conn, tid, basis="worker interrupted (resume pending)")
        return {"entry": {"task": tid, "run": run_id, "step": sid,
                          "to": "paused", "interrupted": True}, "stop": True}
    if status != "finished":
        model.unstick_step(conn, sid)  # release the claim: a ready->running retry must find it dispatchable
        transitions.transition(conn, tid, "failed", basis=f"run {run_id} {status}")
        return {"entry": {"task": tid, "run": run_id, "to": "failed"}, "stop": False}
    return None


def run_cycle(conn, *, scope: str, worker_fn, review_fn, max_steps: int = 50) -> list[dict]:
    log = []
    for _ in range(max_steps):
        perm = model.get_permission(conn, scope)
        if not perm["enabled"]:
            log.append({"stop": "permission off"})
            return log
        candidates = Q.eligible(conn, scope)
        if not candidates:
            log.append({"stop": "queue empty"})
            return log
        # one run at a time per task: a task whose run is still open (an external
        # worker is mid-step, or a crashed run not yet reconciled) is never given a
        # second concurrent run — step over it and take the next candidate. The open
        # run is left untouched (no mid-run kill).
        candidates = [t for t in candidates if not Q.has_open_run(conn, t["task_id"])]
        if not candidates:
            log.append({"stop": "all candidates have an open run"})
            return log
        task = candidates[0]
        if task["state"] != "running":
            # entering running is itself guarded (contract/permission/budgets)
            try:
                transitions.transition(conn, task["task_id"], "running",
                                       basis="autonomous loop", permission=perm)
            except ValueError as e:
                log.append({"skip": task["task_id"], "reason": str(e)})
                return log
        step = Q.next_step(conn, task["task_id"])
        if step is None:
            total = conn.execute("SELECT COUNT(*) c FROM ctl_step WHERE task_id=?",
                                 (task["task_id"],)).fetchone()["c"]
            # An in_progress step whose run is closed (crash between artifact and
            # finish_step, dogfood 22.10 task-f394af6891) is unfinished work: the
            # claim is released instead of marching into verifying with it.
            if conn.execute("SELECT 1 FROM ctl_step WHERE task_id=? AND status='in_progress' LIMIT 1",
                            (task["task_id"],)).fetchone() is not None \
                    and not Q.has_open_run(conn, task_id=task["task_id"]):
                for o in conn.execute("SELECT step_id FROM ctl_step WHERE task_id=? AND status='in_progress'",
                                      (task["task_id"],)).fetchall():
                    model.unstick_step(conn, o["step_id"])
                log.append({"task": task["task_id"], "orphan_steps_released": True})
                continue
            if total == 0:
                transitions.transition(conn, task["task_id"], "blocked",
                                       basis="empty plan: add steps before running")
                log.append({"task": task["task_id"], "to": "blocked",
                            "reason": "empty plan"})
                continue
            # A preempted (paused) step is NOT "all done": the task parks until the step is
            # resumed, instead of marching into verifying with unfinished work.
            if conn.execute("SELECT 1 FROM ctl_step WHERE task_id=? AND status='paused' LIMIT 1",
                            (task["task_id"],)).fetchone() is not None:
                transitions.transition(conn, task["task_id"], "paused",
                                       basis="preempted step awaiting resume")
                log.append({"task": task["task_id"], "to": "paused",
                            "reason": "preempted step"})
                return log
            transitions.transition(conn, task["task_id"], "verifying",
                                   basis="all steps done")
            outcome = finish_verifying(conn, task["task_id"], review_fn=review_fn)
            log.append({"task": task["task_id"], "to": outcome})
            continue
        # dispatch-time barrier: pause arriving after selection is caught here (§7 race)
        ok, why = barrier.check(conn, scope=scope, origin="autonomous",
                                task_id=task["task_id"])
        if not ok:
            transitions.transition(conn, task["task_id"], "paused",
                                   basis=f"loop barrier: {why}")
            log.append({"task": task["task_id"], "to": "paused", "reason": why})
            return log
        run_id = model.start_run(conn, task_id=task["task_id"], step_id=step["step_id"],
                                 contract_version=task["contract_version"],
                                 execution_epoch=perm["execution_epoch"],
                                 worker_id="loop")
        model.add_budget(conn, task["task_id"], "tool_calls", 0)  # ensure counter row
        model.set_run_status(conn, run_id, "running")
        # Claim the step BEFORE dispatch so an out-of-band preempt has something to
        # CAS against (in_progress -> paused) while the worker is mid-turn. A lost claim
        # (already in_progress/paused under another writer) is requeued and rescanned.
        claim = model.mark_step_in_progress(conn, step["step_id"])
        if not claim["ok"]:
            model.set_run_status(conn, run_id, "interrupted")
            log.append({"task": task["task_id"], "run": run_id,
                        "skip_step": step["step_id"], "stored": claim["stored"]})
            continue
        # Re-anchoring: a step that resumes after a preempt gets the stored handoff
        # ("чем занимался, на какой тулзе встал") — a cheap resume, no re-narration.
        handoff = model.paused_handoff_note(conn, step["step_id"])
        step = {**dict(step), "handoff": handoff} if handoff else dict(step)
        try:
            res = worker_fn({"run_id": run_id}, dict(task), step)
        except Exception as exc:
            res = {"status": "failed", "artifacts": [], "error": str(exc)}
        # budget consumed by children is accounted via post_tool_call hook; loop adds its step
        model.add_budget(conn, task["task_id"], "steps", 1)
        stop = _resolve_worker_outcome(conn, task=task, step=step, run_id=run_id, res=res)
        if stop is not None:
            log.append(stop["entry"])
            if stop["stop"]:
                return log
            continue
        ah = _artifact_hash(res.get("artifacts", []))
        model.add_artifact(conn, task["task_id"], run_id, "step_output",
                           {"hash": ah, "items": res.get("artifacts", [])})
        verdict = {"verdict": "pass", "detail": "not checkpoint"}
        if step.get("checkpoint", 1):
            verdict = review_fn({"run_id": run_id}, step, res.get("artifacts", []))
            model.add_review(conn, step["step_id"], run_id, ah,
                             verdict.get("verdict", "revise"), "controller-loop",
                             verdict.get("detail", ""))
        if verdict.get("verdict") == "pass":
            model.finish_step(conn, step["step_id"])
            log.append({"task": task["task_id"], "run": run_id,
                        "step": step["step_id"], "review": "pass"})
            continue
        # checkpoint failed -> step goes back to pending; task needs a fix iteration
        log.append({"task": task["task_id"], "run": run_id, "review": verdict})
        model.unstick_step(conn, step["step_id"])
        if verdict.get("verdict") == "needs_human":
            transitions.transition(conn, task["task_id"], "needs_human",
                                   basis=f"review: {verdict.get('detail')}")
            return log
        # 'revise': give the task one more attempt at the same step (attempt>1 next pick)
        if model.budget_exhausted(conn, task["task_id"], "steps"):
            transitions.transition(conn, task["task_id"], "blocked",
                                   basis="budget: steps exhausted on revise")
            return log
    log.append({"stop": "max_steps"})
    return log


def _backfill_missing_reviews(conn, task_id: str, review_fn) -> int:
    """Checkpoint steps closed in past cycles — or by paths that never write
    ctl_review — were refused by the currency guard forever (dogfood 22.10:
    task-b78849c10d parked verifying<->needs_human on '3/5 checkpoint steps').
    Re-review them with the SAME review_fn the live cycle uses, bound to the
    step's latest artifact; a non-pass verdict is persisted honestly (the
    guard then parks on its real content, not on an absent row)."""
    import hashlib
    import json
    n = 0
    rows = conn.execute(
        "SELECT s.* FROM ctl_step s WHERE s.task_id=? AND s.checkpoint=1 AND s.status='done'"
        " AND NOT EXISTS (SELECT 1 FROM ctl_review r WHERE r.step_id=s.step_id)",
        (task_id,)).fetchall()
    for s in rows:
        run = conn.execute("SELECT * FROM ctl_run WHERE step_id=? ORDER BY rowid DESC LIMIT 1",
                           (s["step_id"],)).fetchone()
        if run is None:
            continue  # step closed without any run: no artifact to review — human decides
        art = conn.execute(
            "SELECT payload FROM ctl_artifact_revision WHERE run_id=? ORDER BY created_at DESC LIMIT 1",
            (run["run_id"],)).fetchone()
        items = json.loads(art["payload"]).get("items", []) if art else []
        ah = (json.loads(art["payload"]).get("hash") if art else None) or hashlib.sha256(
            json.dumps(items, sort_keys=True).encode()).hexdigest()
        v = review_fn({"status": "finished", "artifacts": items}, dict(s), items)
        model.add_review(conn, step_id=s["step_id"], run_id=run["run_id"], artifact_hash=ah,
                         verdict=v.get("verdict", "needs_human"),
                         detail=str(v.get("detail", ""))[:4000], reviewer="review-backfill")
        n += 1
    return n


def finish_verifying(conn, task_id: str, review_fn=None) -> str:
    """verifying -> completed|ready|needs_human by the review-currency guard outcome."""
    # Transient-review race, prod-verified 19.10 (x3): the last checkpoint review
    # can commit AFTER this guard's first SELECT (the judge LLM call inside
    # review_fn can outlive the step-done transition of a parallel dispatch —
    # transition 07:52:53 vs review row 07:53:29 on task-6da178e8ee). Refusing
    # outright parks the task in needs_human for a human to unstick what will
    # self-resolve; one delayed re-check absorbs the ordering without weakening
    # the guard: persistent "missing reviews" still ends in needs_human.
    outcome = _finish_verifying_once(conn, task_id, allow_retry=True)
    if outcome == "retry":
        time.sleep(5)
        outcome = _finish_verifying_once(conn, task_id, allow_retry=False)
    if outcome == "needs_human-backfill":
        if review_fn is not None and _backfill_missing_reviews(conn, task_id, review_fn):
            outcome = _finish_verifying_once(conn, task_id, allow_retry=False)
        if outcome == "needs_human-backfill":
            transitions.transition(
                conn, task_id, "needs_human",
                basis="verify: missing reviews (no runnable artifact to backfill)")
            outcome = "needs_human"
    return outcome


def _finish_verifying_once(conn, task_id: str, *, allow_retry: bool) -> str:
    """One attempt: completed | needs_human, or 'retry' when the refusal was
    specifically about reviews that may still be in flight."""
    try:
        transitions.transition(conn, task_id, "completed", basis="all reviews current")
        return "completed"
    except ValueError as e:
        msg = str(e)
        if "missing reviews" in msg and allow_retry:
            return "retry"
        if "missing reviews" in msg:
            # allow_retry=False: caller may remediate via review backfill before
            # parking (finish_verifying handles it); parking stays the fallback.
            return "needs_human-backfill"
        if "review" in msg:
            transitions.transition(conn, task_id, "needs_human", basis=f"verify: {msg}")
            return "needs_human"
        raise

"""controller.workers — real worker/review bindings (increment 3).

worker_fn: a headless hermes one-shot subprocess inside the task's workdir.
  Why not delegate_task children: dispatch needs a live parent agent turn; the
  pump runs between/without turns. The worker session itself is a full session,
  so fork-guardrails apply to it, and any delegation it does is supervisor-gated.
  The task/step brief is written to a query file (nothing shell-interpreted);
  HERMES_CONTROLLER_TASK_ID marks the process so budgets can be attributed.

review_fn: supervisor judge, but FAIL CLOSED (judge.review fails open for the
  checkpoint path; for task completion an unreachable judge must block, not pass).
  If the contract carries a validation command, it must have passed in the worker
  transcript file first — code, not vibes (spec §29-30).
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import time

logger = logging.getLogger(__name__)

# Windows-only spawn flags (subprocess exposes them on nt only).
CREATE_NEW_PROCESS_GROUP = 0x00000200
DETACHED_PROCESS = 0x00000008


def _spawn_kwargs() -> dict:
    """Detached-spawn kwargs per OS. On POSIX start_new_session gives the worker
    its own session so killpg can tree-kill it. On Windows start_new_session is
    silently IGNORED by CPython, so the equivalent isolation is
    CREATE_NEW_PROCESS_GROUP|DETACHED_PROCESS via creationflags."""
    if os.name == "nt":
        return {"creationflags": CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS}
    return {"start_new_session": True}


def _kill_tree(proc) -> None:
    """Kill the timed-out worker and every grandchild still writing into the
    workdir. POSIX: the spawn put it in its own session -> killpg. Windows: no
    killpg/getpgid exist at all (AttributeError would escape the except) ->
    taskkill /T /F walks the process tree."""
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True, timeout=15, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, AttributeError):
        pass


def kill_worker_tree(pid: int, pgid: int | None = None) -> bool:
    """SIGKILL a worker by pid/pgid (the pump_lock takeover's reach: the
    holder's Popen handle lives in ANOTHER process, only numbers survive).
    True when at least one signal landed."""
    if pgid is None:
        try:
            pgid = os.getpgid(pid)
        except (OSError, AttributeError):
            pgid = None
    if pgid is not None and pgid != 0:
        try:
            os.killpg(pgid, signal.SIGKILL)
            return True
        except (ProcessLookupError, PermissionError):
            pass
    try:
        os.kill(pid, signal.SIGKILL)
        return True
    except OSError:
        return False


def _preempt_signal(conn, *, run_id: str, step_id: str | None) -> bool:
    """True when an out-of-band preempt landed on this run/step since dispatch:
    the scheduler either flagged the run row ``interrupt_requested`` (the durable
    fallback path — used when the gateway control socket has no session verbs, old
    gateway, or the worker is a subprocess with no socket at all), or flipped the
    claimed step ``in_progress -> paused`` via the CAS pause (PR-A model). Either is
    a command to cut the worker short WITHOUT finishing the turn. DB hiccups fail
    open (keep the worker running) — a missed poll is retried next interval."""
    try:
        row = conn.execute(
            "SELECT (SELECT run_status FROM ctl_run WHERE run_id=?) rs,"
            " (SELECT status FROM ctl_step WHERE step_id=?) ss",
            (run_id, step_id or "")).fetchone()
    except sqlite3.Error:
        logger.debug("preempt signal read failed (fail-open, retried next poll)",
                     exc_info=True)
        return False
    return bool(row and (row["rs"] == "interrupt_requested"
                         or (step_id and row["ss"] == "paused")))


def _await_worker(proc, *, conn, run_id: str, step_id: str | None,
                  timeout_s: float | None, poll_s: float, grace_s: float = 60.0,
                  on_poll=None):
    """Bounded wait for the worker with out-of-band preempt polling.

    ``timeout_s=None`` removes the wall-clock bound entirely: the wait is only
    interrupted by completion or a preempt. When a budget IS set and spent with
    no preempt, raises ``subprocess.TimeoutExpired`` (the caller converts it to
    the same graceful pause a preempt gets — see make_worker).

    Returns ``(stdout, stderr, preempted)``. Repeated ``communicate()`` calls are
    safe since bpo-39531: partial pipe data is kept in the Popen buffers between
    calls, so the final call still returns everything the worker printed."""
    deadline = None if timeout_s is None else time.monotonic() + timeout_s + grace_s
    while True:
        if on_poll is not None:
            on_poll()
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            raise subprocess.TimeoutExpired(getattr(proc, "args", ""), timeout_s or 0)
        try:
            out, err = proc.communicate(timeout=poll_s if remaining is None else min(poll_s, remaining))
            return out, err, False
        except subprocess.TimeoutExpired:
            if _preempt_signal(conn, run_id=run_id, step_id=step_id):
                _kill_tree(proc)
                # After the kill every pipe reaches EOF; this collects the buffered
                # partial output too (see bpo-39531 note above).
                out, err = proc.communicate()
                return out, err, True


def scope_env(task) -> dict:
    """Stage003 step 2: the memory scope of a worker run, as spawn env.

    A worker's default write target is ITS OWN task scope («отсутствие
    указанного scope не является разрешением на глобализацию»); the project id
    rides along for the task→project inheritance chain. The central pump
    session has no task: absent vars mean the global policy scope at the read
    side (step 4 injection).
    """
    def field(key: str) -> str:
        try:
            return task[key] or ""
        except (KeyError, IndexError):
            return ""
    return {"HERMES_SCOPE_KIND": "task",
            "HERMES_SCOPE_ID": field("task_id"),
            "HERMES_PROJECT_ID": field("project_id")}


def make_worker(conn, *, timeout_s: float | None = None,
                hermes_bin: str | None = None, extra_env: dict | None = None,
                preempt_poll_s: float = 2.0, grace_s: float = 60.0,
                model_id: str | None = None, provider: str | None = None,
                on_poll=None):
    # model_id/provider (config: controller.worker_model / worker_provider) route
    # background steps to a dedicated endpoint (e.g. an idle GPU) so queue work
    # never competes with the interactive model. Unset -> inherit the default,
    # exactly the pre-routing behavior.
    # Resolve the OS branch once at binding time: the host OS cannot change
    # mid-run, and the tests that fake os.name='nt' must not have it leak into
    # stdlib internals (tempfile checks os.name too).
    spawn_kwargs = _spawn_kwargs()
    def worker_fn(run, task, step):
        brief = _brief(conn, task, step)
        qf = tempfile.NamedTemporaryFile("w", suffix=".md", delete=False,
                                         encoding="utf-8")
        try:
            qf.write(brief)
            qf.close()
            exe = hermes_bin or shutil.which("hermes") or "hermes"
            env = {**os.environ, "HERMES_CONTROLLER_TASK_ID": task["task_id"],
                   "HERMES_CONTROLLER_RUN_ID": run["run_id"],
                   "HERMES_CONTROLLER_STEP_ID": step["step_id"], **scope_env(task)}
            if extra_env:
                env.update(extra_env)
            argv = [exe, "chat", "--query-file", qf.name, "--oneshot", "-Q",
                    "--in", task["workdir"]]
            if timeout_s is not None:
                # None = no wall-clock cap: the step runs to completion unless an
                # out-of-band preempt lands. A budget is opt-in (per-binding), not
                # a habit-default (22.10: 12 healthy runs tree-killed at 31 min).
                argv += ["--run-budget", str(timeout_s)]
            if provider:
                argv += ["--provider", provider]
            if model_id:
                argv += ["-m", model_id]
            # start_new_session: the worker's own grandchildren (terminal/browser tools)
            # must die WITH it — SIGKILL of the direct child alone left orphans writing
            # into the workdir after the pump marked the step failed (review finding 4).
            proc = subprocess.Popen(
                argv,
                cwd=task["workdir"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", env=env,
                **spawn_kwargs)
            from controller import model
            try:
                pgid = os.getpgid(proc.pid)
            except (OSError, AttributeError):
                pgid = None
            try:
                model.record_run_process(conn, run["run_id"], proc.pid, pgid)
            except sqlite3.Error as exc:  # locked writer: adoption/takeover lose the number, the drain loses nothing
                logger.warning("controller worker: run %s pid record failed: %s", run["run_id"], exc)
            try:
                # Bounded wait with preempt polling: communicate() may be re-entered on
                # timeout (CPython keeps partial pipe data in internal buffers and the
                # final call returns everything collected). Each poll costs one indexed
                # read of the run/step rows; the worker is cut short the moment an
                # out-of-band preempt lands, WITHOUT letting the turn finish (acceptance:
                # the pump removes the in_progress step mid-flight).
                out, err, preempted = _await_worker(
                    proc, conn=conn, run_id=run["run_id"],
                    step_id=step.get("step_id"), timeout_s=timeout_s,
                    poll_s=preempt_poll_s, grace_s=grace_s, on_poll=on_poll)
                returncode = proc.returncode
            except subprocess.TimeoutExpired:
                # Budget (explicitly configured) exhausted: take the SAME graceful
                # path an out-of-band preempt takes — pause the step, keep the
                # partial output as the artifact, hand off. The legacy behavior
                # (tree-kill, empty artifacts, status failed) made the judge
                # review an emptiness and the redispatch start with amnesia.
                from controller import model
                _kill_tree(proc)
                out, err = proc.communicate()  # health: allow HX006 -- after killpg every pipe is at EOF; this call only drains the bpo-39531 buffers, same contract as the preempt branch above
                handoff = ""
                sid = step.get("step_id")
                if sid:
                    model.pause_step(conn, sid, task_id=task["task_id"],
                                     note=f"budget {timeout_s}s exhausted; partial work preserved")
                    handoff = model.paused_handoff_note(conn, sid) or "budget exhausted (note not stored)"
                return {"status": "interrupted",
                        "artifacts": [{"stdout_tail": (out or "")[-2000:],
                                       "returncode": proc.returncode}],
                        "handoff": handoff, "detail": (err or "")[-500:]}
            if preempted:
                from controller import model
                handoff = ""
                if step.get("step_id"):
                    handoff = model.paused_handoff_note(conn, step["step_id"])
                return {"status": "interrupted",
                        "artifacts": [{"stdout_tail": (out or "")[-2000:],
                                       "returncode": returncode}],
                        "handoff": handoff or "preempted out-of-band (no note stored)",
                        "detail": (err or "")[-500:]}
            artifacts = [{"stdout_tail": (out or "")[-2000:],
                          "returncode": returncode}]
            status = "finished" if returncode == 0 else "failed"
            return {"status": status, "artifacts": artifacts,
                    "detail": (err or "")[-500:]}
        finally:
            try:
                os.unlink(qf.name)
            except OSError:
                pass
    return worker_fn


def _consequences(conn, task, *, window_s: float = 14 * 86400, cap: int = 8,
                  max_chars: int = 1200) -> str:
    """S2.4 (ТЗ thinking-enable): consequences into the brief — the machine
    version of cautionary §7. Walks the causal subgraph `about`/`causes` around
    the task (writer: model.emit) and appends the last reviews' detail, so a
    worker sees WHAT HAPPENED AROUND this task ("мой стоп породил вторую
    аварию", "внешний агент снял interim-патч") instead of only the contract.
    Hard caps keep this O(recent-window), never O(store); silent on empty."""
    tid = task["task_id"]
    lines: list[str] = []
    rows = conn.execute(
        "SELECT e.event_id, e.kind, e.ts, e.payload FROM ctl_causal_link cl"
        " JOIN ctl_event e ON e.event_id = substr(cl.to_event, 5)"
        " WHERE cl.from_event = ? AND cl.kind IN ('about','causes')"
        "   AND cl.ts > ? ORDER BY cl.ts DESC LIMIT ?",
        (f"task:{tid}", time.time() - window_s, cap)).fetchall()
    from collections import Counter
    per_kind: dict[str, tuple] = {}
    counts: Counter[str] = Counter()
    for _eid, kind, ts, payload in rows:
        counts[kind] += 1
        if kind not in per_kind:  # rows are ts-DESC: first per kind is the freshest
            summ = ""
            try:
                p = json.loads(payload) if payload else {}
                summ = str(p.get("reason") or p.get("detail") or p.get("note")
                           or p.get("basis") or "")[:110]
            except (ValueError, TypeError):
                pass
            per_kind[kind] = (ts, summ)
    for kind, (ts, summ) in sorted(per_kind.items(),
                                   key=lambda kv: -kv[1][0]):
        age_h = max(0.0, (time.time() - ts)) / 3600.0
        n = counts[kind]
        lines.append(f"- {age_h:.0f}h назад: {kind}" + (f" x{n}" if n > 1 else "")
                     + (f" — {summ}" if summ else ""))
    revs = conn.execute(
        "SELECT r.verdict, r.detail, s.title FROM ctl_review r"
        " JOIN ctl_step s ON s.step_id = r.step_id"
        " WHERE s.task_id = ? ORDER BY r.created_at DESC LIMIT 3",
        (tid,)).fetchall()
    for verdict, detail, title in revs:
        lines.append(f"- review «{(title or '')[:40]}»: {verdict}"
                     + (f" — {str(detail or '')[:140]}" if detail else ""))
    if not lines:
        return ""
    return "\n".join(lines)[:max_chars]


def _brief(conn, task, step) -> str:
    from controller import model
    c = model.current_contract(conn, task["task_id"])
    lines = [
        f"# Controlled step ({step['kind']}): "
        f"{(dict(step).get('brief') or '').strip() or step['title']}",
        f"Task: {task['goal']}",
        "",
        "Acceptance criteria:", *[f"- {a}" for a in c.get("acceptance", []) or ["(none listed)"]],
    ]
    # S1.3 (TЗ thinking-enable): the machine predicate is part of the brief —
    # the worker sees WHAT must hold, not only the prose criteria.
    pred = contract_validation_cmd(conn, task["task_id"])
    if pred:
        lines.append(f"Machine predicate (must pass in the workdir before completion): {pred}")
    lines += [
        "Confirmed facts:", *[f"- {a}" for a in (c.get("facts") or [])[:20]],
        "Restrictions:", *[f"- {a}" for a in (c.get("restrictions") or [])[:20]],
    ]
    # S2.4: what happened around this task (causal graph + last reviews), before
    # the worker starts touching things. Empty ledger adds nothing.
    cons = _consequences(conn, task)
    if cons:
        lines += ["", "Recent consequences around this task (causal graph):", cons]
    lines += [
        "",
        "Work ONLY on this step. When done, state exactly how each acceptance",
        "criterion is verified (command output). This run is checkpointed.",
    ]
    # Cheap resume (acceptance #4): a step that was preempted re-dispatches with the
    # handoff line the preempter stored ("чем занимался, на какой тулзе встал").
    # The contract fields above are re-read NOW, so a contract edit that landed during
    # the pause re-anchors the resumed run instead of replaying stale context.
    note = str(step.get("handoff") or "").strip()
    if note:
        lines += [
            "",
            "## RESUME — this step was preempted mid-turn before",
            f"Handoff from the interrupted run: {note}",
            "Do NOT redo finished work: continue from the handoff point, and",
            "re-anchor first on the CURRENT acceptance criteria/facts above.",
        ]
    return "\n".join(lines)


def _diffstat(workdir: str, max_chars: int = 1500) -> str:
    """What the run actually changed (S2.5): working-tree diffstat + file list.
    Empty string on any problem (not a repo, no git, dirty index) — the judge
    then grades on what it has, it never crashes on missing evidence."""
    import subprocess
    try:
        st = subprocess.run(["git", "-C", workdir, "diff", "--stat"],
                            capture_output=True, text=True, timeout=20, check=False)
        un = subprocess.run(["git", "-C", workdir, "status", "--porcelain"],
                            capture_output=True, text=True, timeout=20, check=False)
        out = ((st.stdout or "") + ("\nUNTRACKED/CHANGED:\n" + un.stdout
                                    if (un.stdout or "").strip() else ""))
        return out.strip()[:max_chars]
    except Exception:
        return ""


def make_review(conn, *, judge_cfg: dict | None = None):
    from controller import model
    def review_fn(run, step, artifacts):
        tail = ""
        if artifacts and isinstance(artifacts[0], dict):
            tail = artifacts[0].get("stdout_tail", "")
        if judge_cfg is None:
            return {"verdict": "needs_human", "detail": "judge not configured"}
        try:
            from plugins.supervisor.judge import review as _j
            # S2.5 (ТЗ thinking-enable): the judge sees meaning, not the tail.
            # The population it grades against is the contract's acceptance
            # criteria (fixed, owner-declared — src/claims shape); the run
            # only reports which criteria its evidence covers.
            tid = str(step.get("task_id") or "")
            c = model.current_contract(conn, tid) if tid else {}
            criteria = [str(a) for a in (c.get("acceptance") or []) if isinstance(a, str)]
            facts = [str(f)[:160] for f in (c.get("facts") or [])[:10]]
            workdir = ""
            if tid:
                trow = model.get_task(conn, tid)
                workdir = str(dict(trow).get("workdir") or "") if trow else ""
            evidence = tail
            stat = _diffstat(workdir) if workdir else ""
            if stat:
                evidence = f"DIFFSTAT (working tree):\n{stat}\n\nSTDOUT:\n{tail}"
            hist = ([{"tool": "worker", "status": str(artifacts[0].get("returncode")),
                       "args": step.get("title", ""), "result": evidence[:8000]}]
                    if artifacts else [])
            if facts:
                hist = hist + [{"tool": "contract-facts", "status": "given",
                                "args": "confirmed before this run",
                                "result": "\n".join(facts)}]
            v = _j(judge_cfg, step.get("title", "step"), hist,
                   criteria=criteria or None)
            covered = v.get("covered")
            verdict = v.get("verdict", "revise")
            # judge.review is fail-OPEN by design (advisory checkpoint); completion needs
            # fail-closed. degraded is judge.py's machine flag (HTTP down, unparsable/empty
            # completion, missing verdict field, off-whitelist verdict coerced to pass);
            # the wording check covers judges older than the degraded flag; a pass with no
            # reason sentence violates JUDGE_SYSTEM's contract (a real pass always explains).
            reason = v.get("reason") or ""
            if verdict == "pass" and (v.get("degraded") or "judge unavailable" in reason
                                      or not reason.strip()):
                return {"verdict": "needs_human",
                        "detail": reason or "judge pass without reason (degraded)"}
            # code-side gate: worker must have exited clean for a pass
            if verdict == "pass" and artifacts and artifacts[0].get("returncode") not in (0, None):
                return {"verdict": "revise", "detail": "judge pass but worker exit != 0"}
            # S2.5 counterexample gate (ТЗ): a pass that cannot name a single
            # acceptance criterion its evidence verifies is exactly the vacuous
            # shape the machine predicate passed but the meaning did not. The
            # predicate being green does NOT excuse empty coverage.
            if verdict == "pass" and criteria and not covered:
                return {"verdict": "revise",
                        "detail": "judge pass with zero acceptance-criteria "
                                  "coverage — name the criteria the evidence verifies",
                        "criteria_covered": []}
            return {"verdict": verdict, "detail": v.get("reason", ""),
                    "criteria_covered": covered}
        except Exception as exc:  # FAIL CLOSED unlike checkpoint judge
            return {"verdict": "needs_human", "detail": f"judge unavailable: {exc}"}
    return review_fn


def contract_validation_cmd(conn, task_id: str) -> str:
    """The contract's machine predicate (S1.3, TЗ thinking-enable).

    Canonical shape: top-level ``validation_cmd`` (``acceptance`` stays the list
    of acceptance criteria). Legacy dict-shaped ``acceptance`` carrying the
    command is honoured, so persisted contracts in that shape keep working."""
    from controller import model
    c = model.current_contract(conn, task_id)
    cmd = c.get("validation_cmd")
    if not cmd and isinstance(c.get("acceptance"), dict):
        cmd = c["acceptance"].get("validation_cmd")
    return str(cmd or "")


def run_validation(conn, task_id: str, workdir: str) -> tuple[bool, str]:
    """Contract-level gate: acceptance.validation_cmd must pass (code, not prose)."""
    cmd = contract_validation_cmd(conn, task_id)
    if not cmd:
        return True, "no validation_cmd in contract"
    env = dict(os.environ, HERMES_TASK_ID=task_id)
    p = subprocess.run(cmd, shell=True, cwd=workdir, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=600, check=False,
                       env=env)
    return p.returncode == 0, (p.stdout + p.stderr)[-1000:]

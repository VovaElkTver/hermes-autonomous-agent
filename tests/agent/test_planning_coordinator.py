"""Delivery-2 tests: PlanningCoordinator auto-start before execution.

Acceptance (v0.6 A10-A15 / doc §13): always-mode plans a working request without /plan;
planner failure stays visible; recursive admissions short-circuit; service turns skip;
every outcome journalled; off-mode byte-for-byte untouched.
"""

import json
import sys

sys.path.insert(0, "/home/qwen/hermes-agent")

import agent.planning_coordinator as pc
from types import SimpleNamespace


class Cfg:
    def __init__(self, approved_tasks):
        self.approved_tasks = approved_tasks
        self.calls = []

    def plan(self, *, task):
        self.calls.append(task)
        return self.approved_tasks


APPROVED = {
    "status": "approved",
    "version": 1,
    "plan_ref": "plan_candidate-002",
    "artifact_dir": "/tmp/never-used-in-test",
    "plan": {"approach_ref": "d",
             "tasks": [{"id": "T1", "goal": "g", "requirement_ids": [], "depends_on": [],
                        "inputs": [], "outputs": ["o"], "verification": ["run tests"], "risks": []}]},
}


def make_ctx(**kw):
    base = dict(message="добавь восстановление состояния", session_key="sess-1",
                scheduled_heartbeat=False, voice_turn=False, persist_user_display_kind=None)
    base.update(kw)
    return SimpleNamespace(**base)


def patched(monkeypatch, tmpdir, cfg_extra=None):
    cfg = {"multistage": True, "mode": "always", "runs_dir": str(tmpdir)}
    cfg.update(cfg_extra or {})
    monkeypatch.setattr(pc, "_load_cfg", lambda: cfg)
    return cfg


def install_planner(monkeypatch, planner_obj):
    monkeypatch.setattr(pc, "_build_planner_for", lambda agent, cfg: planner_obj)


def test_prepared_flow_prepends_package_and_journals(monkeypatch, tmpdir):
    patched(monkeypatch, tmpdir)
    import os
    art = tmpdir.mkdir("art")
    open(str(art.join("plan_candidate-002.json")), "w").write(
        json.dumps({"version": 1, "plan": {"tasks": [
            {"id": "T1", "goal": "g", "verification": ["run tests"]}]}}))
    approved = {**APPROVED, "artifact_dir": str(art)}
    planner = Cfg(approved)
    install_planner(monkeypatch, planner)
    ctx = make_ctx()

    pc.maybe_prepare_plan(None, ctx)

    assert ctx.message.startswith("[АКТИВНЫЙ ПЛАН w-0001 v1")
    assert "T1: g [проверка: run tests]" in ctx.message
    assert "добавь восстановление состояния" in ctx.message  # request preserved below the package
    events = [json.loads(l) for l in open(f"{tmpdir}/events.jsonl", encoding="utf-8")]
    names = [e["event"] for e in events]
    assert names == ["planning.started", "planning.completed", "execution.started_with_plan"]
    works = [json.loads(l) for l in open(f"{tmpdir}/works.jsonl", encoding="utf-8")]
    assert works[0]["work_id"] == "w-0001" and works[0]["status"] == "approved"


def test_approved_plan_body_read_from_artifact(tmpdir, monkeypatch):
    art = tmpdir.mkdir("runs").join("x")
    import os
    os.makedirs(str(art))
    payload = {"plan": {"tasks": [{"id": "T3", "goal": "wire", "verification": ["pytest -q"]}]}}

    open(f"{art}/review-003.json", "w").write("{}")
    open(f"{art}/plan_candidate-004.json", "w").write(json.dumps(payload))

    patched(monkeypatch, tmpdir)
    planner = Cfg({**APPROVED, "artifact_dir": str(art)})
    install_planner(monkeypatch, planner)
    ctx = make_ctx()
    pc.maybe_prepare_plan(None, ctx)
    assert "T3: wire [проверка: pytest -q]" in ctx.message


def test_off_mode_leaves_message_untouched(monkeypatch, tmpdir):
    patched(monkeypatch, tmpdir, {"multistage": False})
    ctx = make_ctx()
    pc.maybe_prepare_plan(None, ctx)
    assert ctx.message == "добавь восстановление состояния"


def test_recursive_admission_short_circuits(monkeypatch, tmpdir):
    patched(monkeypatch, tmpdir)
    planner = Cfg(APPROVED)
    install_planner(monkeypatch, planner)

    token = pc.PLANNING_PURPOSE.set("planning")
    try:
        ctx = make_ctx()
        pc.maybe_prepare_plan(None, ctx)
        assert ctx.message == "добавь восстановление состояния"  # untouched...
    finally:
        pc.PLANNING_PURPOSE.reset(token)
    events = [json.loads(l) for l in open(f"{tmpdir}/events.jsonl", encoding="utf-8")]
    assert events[0]["event"] == "planning.skipped_recursive"
    assert planner.calls == []  # ...and no pipeline ran


def test_planner_failure_is_visible_not_silent(monkeypatch, tmpdir):
    patched(monkeypatch, tmpdir)

    class Boom:
        def plan(self, *, task):
            raise RuntimeError("provider unreachable")

    install_planner(monkeypatch, Boom())
    ctx = make_ctx()
    pc.maybe_prepare_plan(None, ctx)
    assert "[планирование не удалось: RuntimeError: provider unreachable" in ctx.message
    assert "исполнение без плана]" in ctx.message
    events = [json.loads(l) for l in open(f"{tmpdir}/events.jsonl", encoding="utf-8")]
    assert any(e["event"] == "planning.failed" for e in events)


def test_needs_input_parks_with_questions(monkeypatch, tmpdir):
    patched(monkeypatch, tmpdir)
    parked = {"status": "needs_input", "questions": ["где живёт state.db?"], "artifact_dir": str(tmpdir)}
    planner = Cfg(parked)
    install_planner(monkeypatch, planner)
    ctx = make_ctx()
    pc.maybe_prepare_plan(None, ctx)
    assert "[планирование приостановлено: статус needs_input" in ctx.message
    assert "где живёт state.db?" in ctx.message
    works = [json.loads(l) for l in open(f"{tmpdir}/works.jsonl", encoding="utf-8")]
    assert works[0]["status"] == "needs_input"


def test_service_turns_skip_without_pipeline(monkeypatch, tmpdir):
    patched(monkeypatch, tmpdir)
    planner = Cfg(APPROVED)
    install_planner(monkeypatch, planner)
    for ctx in (make_ctx(scheduled_heartbeat=True), make_ctx(voice_turn=True),
                make_ctx(persist_user_display_kind="internal_notification"),
                make_ctx(message="/plan something"), make_ctx(message="")):
        pc.maybe_prepare_plan(None, ctx)
    assert planner.calls == []


class AsyncPlanner(Cfg):
    """Regression for the 14.11 live failure: plan() is genuinely async in production."""

    def __init__(self, approved_tasks):
        super().__init__(approved_tasks)

    async def plan(self, *, task):
        self.calls.append(task)
        return self.approved_tasks


def test_async_planner_coroutine_is_driven(monkeypatch, tmpdir):
    patched(monkeypatch, tmpdir)
    planner = AsyncPlanner(APPROVED)
    install_planner(monkeypatch, planner)
    ctx = make_ctx()
    pc.maybe_prepare_plan(None, ctx)
    assert ctx.message.startswith("[АКТИВНЫЙ ПЛАН w-0001 v1")


def test_work_ids_unique_without_prior_record(monkeypatch, tmpdir):
    """EAA Д7 regression: old line-counting returned identical ids when works.jsonl had no rows yet."""
    patched(monkeypatch, tmpdir)
    a = pc._next_work_id(str(tmpdir))
    b = pc._next_work_id(str(tmpdir))
    assert (a, b) == ("w-0001", "w-0002")


def test_service_skip_is_journalled(monkeypatch, tmpdir):
    """EAA Д5 regression: bypassing the pipeline must leave an observable trace."""
    patched(monkeypatch, tmpdir)
    planner = Cfg(APPROVED)
    install_planner(monkeypatch, planner)
    ctx = make_ctx(scheduled_heartbeat=True)
    pc.maybe_prepare_plan(None, ctx)
    events = [json.loads(l) for l in open(f"{tmpdir}/events.jsonl", encoding="utf-8")]
    skips = [e for e in events if e["event"] == "planning.skipped_service"]
    assert len(skips) == 1 and skips[0]["reason"] == "heartbeat"
    assert planner.calls == []


def test_admission_failure_not_silent(monkeypatch, tmpdir):
    """EAA Д4 regression: crash BEFORE plan() is journalled, turn proceeds without a plan."""
    patched(monkeypatch, tmpdir)

    def boom(agent, cfg):
        raise RuntimeError("no credentials")

    monkeypatch.setattr(pc, "_build_planner_for", boom)
    ctx = make_ctx()
    pc.maybe_prepare_plan(None, ctx)  # must not raise
    assert ctx.message == "добавь восстановление состояния"
    events = [json.loads(l) for l in open(f"{tmpdir}/events.jsonl", encoding="utf-8")]
    assert any(e["event"] == "planning.admission_failed" and "no credentials" in e["error"]
               for e in events)

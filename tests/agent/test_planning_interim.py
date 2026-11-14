"""Interim delivery (v0.9): background conveyor + light-model chat, owner spec 14.11.

'чтоб диалог поддерживала лёгкая модель, а темы типа планирования отправлялись
на более тяжёлую; как голосовые ассистенты - быстро ответить/уточнить, ядро в фоне'. 
Measured 14.11: both endpoints ~7ms/tok raw -> split is loop-overhead, not tok/s;
therefore the fix is non-blocking admission, not a second model."""
import json
import sys
import time

sys.path.insert(0, "/home/qwen/hermes-agent")

import agent.planning_coordinator as pc

from tests.agent.test_planning_coordinator import APPROVED, Cfg, install_planner, make_ctx, patched


def test_interim_returns_fast_and_conveyor_lands_pending(monkeypatch, tmpdir):
    patched(monkeypatch, tmpdir, {"mode": "auto", "interim": True})
    art = tmpdir.mkdir("art3")
    open(str(art.join("plan_candidate-001.json")), "w").write(json.dumps(
        {"version": 1, "plan": {"tasks": [{"id": "T1", "goal": "g", "verification": ["v"]}]}}))

    class SlowPlanner(Cfg):
        def plan(self, *, task):
            time.sleep(1.2)  # heavier than the assertion budget below
            return super().plan(task=task)

    planner = SlowPlanner({**APPROVED, "artifact_dir": str(art)})
    install_planner(monkeypatch, planner)
    ctx = make_ctx(message="проанализируй проект и составь план рефакторинга")
    t0 = time.time()
    pc.maybe_prepare_plan(None, ctx)
    assert time.time() - t0 < 0.9  # turn is NOT blocked by the conveyor
    assert "строится в фоне" in ctx.message  # interim instruction to the light model
    assert any(e["event"] == "planning.started_async" for e in _events(tmpdir))
    # worker thread finishes and lands exactly-one pending note
    deadline = time.time() + 5
    penddir = f"{tmpdir}/pending/sess-1"
    while time.time() < deadline and not _pending(penddir):
        time.sleep(0.1)
    names = _pending(penddir)
    assert len(names) == 1
    note = open(f"{penddir}/{names[0]}", encoding="utf-8").read()
    assert "АКТИВНЫЙ ПЛАН w-" in note  # render_work_package_note outcome


def test_next_turn_consumes_pending_note(monkeypatch, tmpdir):
    """The finished background plan surfaces on the NEXT remark, then is gone."""
    pc.PlanningCoordinator._defer_note(f"{tmpdir}/pending/sess-1", "w-0007", "[АКТИВНЫЙ ПЛАН w-0007 v1 — готов]")
    patched(monkeypatch, tmpdir, {"mode": "auto", "interim": True})
    planner = Cfg(APPROVED)
    install_planner(monkeypatch, planner)
    ctx = make_ctx(message="ок")  # trivial remark: direct route, but consumption still happens
    pc.maybe_prepare_plan(None, ctx)
    assert ctx.message.startswith("[АКТИВНЫЙ ПЛАН w-0007 v1 — готов]")
    assert _pending(f"{tmpdir}/pending/sess-1") == []  # exactly-once delivery


def _events(tmpdir):
    return [json.loads(l) for l in open(f"{tmpdir}/events.jsonl", encoding="utf-8")]


def _pending(penddir):
    try:
        return sorted(n for n in __import__("os").listdir(penddir) if n.endswith(".md"))
    except OSError:
        return []

"""Delivery-3 P1a: mode=auto routing behaviour at the coordinator level."""

import json
import os
import sys

sys.path.insert(0, "/home/qwen/hermes-agent")

import agent.planning_coordinator as pc

from tests.agent.test_planning_coordinator import APPROVED, Cfg, install_planner, make_ctx, patched


def _events(tmpdir):
    return [json.loads(l) for l in open(f"{tmpdir}/events.jsonl", encoding="utf-8")]


def test_auto_mode_routes_trivial_around_pipeline(monkeypatch, tmpdir):
    patched(monkeypatch, tmpdir, {"mode": "auto"})
    planner = Cfg(APPROVED)
    install_planner(monkeypatch, planner)
    ctx = make_ctx(message="который час?")
    pc.maybe_prepare_plan(None, ctx)
    assert ctx.message == "который час?"  # untouched: zero pipeline latency
    skips = [e for e in _events(tmpdir) if e["event"] == "planning.skipped_router"]
    assert len(skips) == 1 and skips[0]["reason"]
    assert planner.calls == []


def test_auto_mode_admits_work_request(monkeypatch, tmpdir):
    patched(monkeypatch, tmpdir, {"mode": "auto"})
    art = tmpdir.mkdir("art2")
    open(str(art.join("plan_candidate-001.json")), "w").write(json.dumps(
        {"version": 1, "plan": {"tasks": [{"id": "T9", "goal": "g", "verification": ["v"]}]}}))
    install_planner(monkeypatch, Cfg({**APPROVED, "artifact_dir": str(art)}))
    ctx = make_ctx(message="почини гонку в agent/planning_coordinator.py")
    pc.maybe_prepare_plan(None, ctx)
    assert ctx.message.startswith("[АКТИВНЫЙ ПЛАН w-0001 v1")
    assert any(e["event"] == "planning.router_admitted" for e in _events(tmpdir))


def test_unknown_mode_is_legacy(monkeypatch, tmpdir):
    patched(monkeypatch, tmpdir, {"mode": "surprise"})
    planner = Cfg(APPROVED)
    install_planner(monkeypatch, planner)
    ctx = make_ctx()
    pc.maybe_prepare_plan(None, ctx)
    assert ctx.message == "добавь восстановление состояния"
    assert not os.path.exists(f"{tmpdir}/events.jsonl")
    assert planner.calls == []


def test_auto_inflight_followup_continues_existing_plan(monkeypatch, tmpdir):
    """Owner requirement: 'продолжай' during an active task rides the existing plan."""
    patched(monkeypatch, tmpdir, {"mode": "auto"})
    from agent.conversation_state import save_state

    save_state(str(tmpdir), "sess-1", {"phase": "задача принята", "goal": "fix",
                                       "active_plan_ref": "plan_candidate-004", "depth": 2})
    planner = Cfg(APPROVED)
    install_planner(monkeypatch, planner)
    ctx = make_ctx(message="ок")
    pc.maybe_prepare_plan(None, ctx)
    assert ctx.message == "ок"  # untouched
    events = [json.loads(l) for l in open(f"{tmpdir}/events.jsonl", encoding="utf-8")]
    skips = [e for e in events if e["event"] == "planning.skipped_router"]
    assert skips and "continue_existing_plan" in skips[0]["reason"]
    assert planner.calls == []

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


def test_interim_push_delivers_proactively(monkeypatch, tmpdir):
    """interim_push: conveyor finish pushes via hermes send AND removes pending note."""
    from types import SimpleNamespace
    patched(monkeypatch, tmpdir, {"mode": "auto", "interim": True, "interim_push": True})
    art = tmpdir.mkdir("artp")
    open(str(art.join("plan_candidate-001.json")), "w").write(json.dumps(
        {"version": 1, "plan": {"tasks": [{"id": "T1", "goal": "g", "verification": ["v"]}]}}))
    install_planner(monkeypatch, Cfg({**APPROVED, "artifact_dir": str(art)}))
    sent = []
    monkeypatch.setattr(pc.PlanningCoordinator, "_push_via_cli",
                        staticmethod(lambda target, text: sent.append((target, text)) or True))
    ctx = make_ctx(message="составь план миграции")
    ctx.source = SimpleNamespace(platform="matrix", chat_id="!ROOM:mx.elk.mobi")
    pc.maybe_prepare_plan(None, ctx)
    assert not _pending(f"{tmpdir}/pending/sess-1"), "pushed => no next-turn duplicate"
    deadline = time.time() + 5
    while time.time() < deadline and not sent:
        time.sleep(0.05)
    assert sent and sent[0][0] == "matrix:!ROOM:mx.elk.mobi"
    assert "АКТИВНЫЙ ПЛАН" in sent[0][1]


def test_interim_push_failure_keeps_pending_note(monkeypatch, tmpdir):
    """hermes send fails (rc!=0 / exception) -> pending fallback survives."""
    patched(monkeypatch, tmpdir, {"mode": "auto", "interim": True, "interim_push": True})
    install_planner(monkeypatch, Cfg(APPROVED))
    monkeypatch.setattr(pc.PlanningCoordinator, "_push_via_cli",
                        staticmethod(lambda target, text: False))
    ctx = make_ctx(message="составь план доставки")
    pc.maybe_prepare_plan(None, ctx)
    deadline = time.time() + 5
    penddir = f"{tmpdir}/pending/sess-1"
    while time.time() < deadline and not _pending(penddir):
        time.sleep(0.05)
    assert len(_pending(penddir)) == 1  # next-turn consumption still delivers


def test_notify_target_skips_local(monkeypatch, tmpdir):
    """No platform/chat context -> notify stays None, file-only delivery."""
    patched(monkeypatch, tmpdir, {"mode": "auto", "interim": True, "interim_push": True})
    install_planner(monkeypatch, Cfg(APPROVED))
    seen = {}
    orig = pc.PlanningCoordinator.prepare_async
    def spy(self, *, request_text, session_key="", notify_target=None):
        seen["t"] = notify_target
        return orig(self, request_text=request_text, session_key=session_key,
                    notify_target=notify_target)
    monkeypatch.setattr(pc.PlanningCoordinator, "prepare_async", spy)
    ctx = make_ctx(message="составь план")  # no .source attribute at all
    pc.maybe_prepare_plan(None, ctx)
    assert seen["t"] is None

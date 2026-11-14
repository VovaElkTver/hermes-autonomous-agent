"""Delivery-3 P1 tests: conversation-state estimator (no LLM, file-backed, hysteresis).

R2 acceptance: a scripted trivial dialog routes direct repeatedly; a work request flips
the session to depth>=2; an in-flight trivial follow-up suggests continue, not a new
conveyor. Storage is a per-session FILE under runs_dir/state (restart-safe).
"""

import json
import sys

sys.path.insert(0, "/home/qwen/hermes-agent")

from agent.conversation_state import load_state, route_with_state, save_state


def test_trivial_dialog_stays_direct(tmpdir):
    key = "s1"
    for msg in ("который час?", "ок", "спасибо"):
        route, reason = route_with_state(msg, str(tmpdir), key)
        assert route == "direct", (msg, reason)
    st = load_state(str(tmpdir), key)
    assert st["depth"] == 0


def test_work_request_raises_depth_to_full(tmpdir):
    key = "s2"
    route, reason = route_with_state("почини гонку в agent/planning_coordinator.py и tests/x.py",
                                     str(tmpdir), key)
    assert route == "plan" and "depth" in reason
    # simulated: pipeline ran and stored its plan
    save_state(str(tmpdir), key, {"phase": "задача принята", "goal": "fix race",
                                  "active_plan_ref": "plan_candidate-004", "depth": 2})
    # in-flight trivial follow-up -> continue existing plan, not a new conveyor
    route2, reason2 = route_with_state("продолжай", str(tmpdir), key)
    assert route2 == "continue" and "inflight" in reason2


def test_depth_decay_is_conservative(tmpdir):
    key = "s3"
    save_state(str(tmpdir), key, {"phase": "задача принята", "goal": "g",
                                  "active_plan_ref": "", "depth": 3})
    route, reason = route_with_state("а пока просто уточню: это долго?", str(tmpdir), key)
    assert route == "plan" and "decay2" in reason  # 3 -> 2: still full conveyor
    assert load_state(str(tmpdir), key)["depth"] == 2  # decay persisted
    route2, reason2 = route_with_state("а пока просто уточню: это долго?", str(tmpdir), key)
    assert route2 == "direct" and "decay1" in reason2  # 2 -> 1: below full, cheap again
    assert load_state(str(tmpdir), key)["depth"] == 1


def test_explicit_plan_request_always_plans(tmpdir):
    route, reason = route_with_state("составь план миграции", str(tmpdir), "s4")
    assert route == "plan" and reason == "explicit_plan_request"


def test_stale_state_decays_to_cheap(tmpdir):
    import time

    key = "s5"
    save_state(str(tmpdir), key, {"phase": "задача принята", "depth": 4,
                                  "updated_at": time.time() - 7200})
    route, _ = route_with_state("ок", str(tmpdir), key)
    assert route == "direct"
    st = load_state(str(tmpdir), key)
    assert st["depth"] <= 1  # stale hot session cooled down


def test_corrupt_or_missing_file_is_idle(tmpdir):
    st = load_state(str(tmpdir), "nonexistent")
    assert st["depth"] == 0 and st["phase"] == "idle"
    p = tmpdir.join("state").mkdir().join("bad.json")
    p.write_text("{oops", encoding="utf-8")
    st2 = load_state(str(tmpdir), "bad")
    assert st2["depth"] == 0

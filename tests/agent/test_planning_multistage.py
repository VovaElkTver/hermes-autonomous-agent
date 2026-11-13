"""Integration tests for MultiStagePlanner with a scripted fake runtime.

Covers the first-delivery contract: artifact content flows between stages, format errors are
repaired from the separate format budget, blocking questions park as needs_input, review loop
honours max_revisions, and requirement coverage is checked against CONTRACT.
"""

import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, "/home/qwen/hermes-agent")

from agent.planning_multistage import (
    FileArtifactStore,
    MultiStagePlanner,
)


def run(coro):
    import asyncio
    return asyncio.run(coro)


class Cfg:
    contract_alias = "planner"
    researcher_alias = "research"
    planner_alias = "heavy"
    reviewer_alias = "review"
    max_revisions = 2
    format_retries = 1


def make_runner(script, calls):
    """script: list of callables(role, user_prompt) -> str."""

    def run_stage(*, role, model_alias, system_prompt, user_prompt):
        calls.append({"role": role, "user_prompt": json.loads(user_prompt)})
        fn = script[len(calls) - 1]
        return fn(role, user_prompt)

    return run_stage


CONTRACT_OK = json.dumps({
    "goal": "add restore", "requirements": [{"id": "R1", "description": "restore works",
                                             "acceptance": ["roundtrip test passes"]}],
    "constraints": [], "non_goals": [], "unknowns": [],
}, ensure_ascii=False)
RESEARCH_OK = json.dumps({"facts": [{"claim": "c", "source": "a.py:12"}], "assumptions": [],
                          "unresolved": [], "blocking_questions": []}, ensure_ascii=False)
STRATEGY_OK = json.dumps({"chosen": "reuse kernel snapshot", "reasons": ["cheapest"],
                          "rejected": [{"approach": "files", "reason": "no atomicity"}],
                          "revisit_if": ["concurrent writes required"],
                          "alternatives_considered": True}, ensure_ascii=False)
REVIEW_REJECT = json.dumps({"approved": False, "findings": [
    {"problem": "нет проверки повреждённого состояния", "required_change": "добавить сценарий"}]},
    ensure_ascii=False)
REVIEW_OK = json.dumps({"approved": True, "findings": []}, ensure_ascii=False)


def plan_json(bad_requirement="R9"):
    return json.dumps({
        "approach_ref": "decision-001",
        "tasks": [{"id": "T1", "goal": "g", "requirement_ids": [bad_requirement], "depends_on": [],
                   "inputs": [], "outputs": ["o"], "verification": ["run tests"], "risks": []}],
    }, ensure_ascii=False)


PLAN_OK = plan_json("R1")


def test_happy_path_artifacts_flow_and_autoapprove(tmpdir):
    calls = []
    script = [
        lambda r, u: CONTRACT_OK,
        lambda r, u: RESEARCH_OK,
        lambda r, u: STRATEGY_OK,
        lambda r, u: PLAN_OK,
        lambda r, u: REVIEW_OK,
    ]
    store = FileArtifactStore(str(tmpdir))
    planner = MultiStagePlanner(config=Cfg(), run_stage=make_runner(script, calls), store=store)
    result = run(planner.plan(task="восстановление состояния"))

    assert result["status"] == "approved", result
    # no human gate between REVIEW and approved
    plan_call = next(c for c in calls if c["role"] == "planner" and "contract" in c["user_prompt"]["inputs"]
                     and "strategy" in c["user_prompt"]["inputs"])
    # concrete CONTENT of prior artifacts reached next stage (not just IDs)
    assert plan_call["user_prompt"]["inputs"]["contract"]["requirements"][0]["id"] == "R1"
    assert plan_call["user_prompt"]["inputs"]["strategy"]["chosen"] == "reuse kernel snapshot"
    # review saw the concrete plan version
    review_call = next(c for c in calls if c["role"] == "reviewer")
    assert review_call["user_prompt"]["inputs"]["plan"]["version"] == 1
    idx = open(os.path.join(store.run_dir, "index.jsonl"), encoding="utf-8").read()
    assert '"event": "reviewed"' in idx


def test_coverage_error_repaired_from_format_budget(tmpdir):
    calls = []
    # attempt 1: coverage error (R9), attempt 2 (format retry): fixed to R1 -> passes
    script = [
        lambda r, u: CONTRACT_OK,
        lambda r, u: RESEARCH_OK,
        lambda r, u: STRATEGY_OK,
        lambda r, u: plan_json("R9"),
        lambda r, u: PLAN_OK,
        lambda r, u: REVIEW_OK,
    ]
    store = FileArtifactStore(str(tmpdir))
    planner = MultiStagePlanner(config=Cfg(), run_stage=make_runner(script, calls), store=store)
    result = run(planner.plan(task="x"))
    assert result["status"] == "approved"
    # second PLAN call must contain validation feedback referencing consistency
    repair_call = calls[4]["user_prompt"]
    assert repair_call["validation_errors"][0]["type"] == "artifact_consistency"
    assert "T1: неизвестные требования ['R9']" in repair_call["validation_errors"][0]["message"]


def test_format_error_uses_format_budget_not_max_revisions(tmpdir):
    bad = "{not json"          # broken JSON twice -> format budget exhausted
    calls = []
    script = [
        lambda r, u: CONTRACT_OK,
        lambda r, u: RESEARCH_OK,
        lambda r, u: STRATEGY_OK,
        lambda r, u: bad,
        lambda r, u: bad,
        # never reaches REVIEW: needs_revision at plan stage
    ]
    store = FileArtifactStore(str(tmpdir))
    planner = MultiStagePlanner(config=Cfg(), run_stage=make_runner(script, calls), store=store)
    result = run(planner.plan(task="x"))
    assert result["status"] == "needs_revision" and result["stage"] == "plan"
    roles = [c["role"] for c in calls]
    assert roles.count("reviewer") == 0  # meaning-budget untouched


def test_blocking_questions_park_as_needs_input(tmpdir):
    research = json.dumps({"facts": [], "assumptions": [], "unresolved": [],
                           "blocking_questions": ["где живёт state.db?"]}, ensure_ascii=False)
    calls = []
    script = [lambda r, u: CONTRACT_OK, lambda r, u: research]
    store = FileArtifactStore(str(tmpdir))
    planner = MultiStagePlanner(config=Cfg(), run_stage=make_runner(script, calls), store=store)
    result = run(planner.plan(task="x"))
    assert result["status"] == "needs_input"
    assert result["questions"] == ["где живёт state.db?"]
    roles = [c["role"] for c in calls]
    assert "planner-plan" not in roles and roles.count("reviewer") == 0


def test_review_loop_exhausts_max_revisions_then_needs_revision(tmpdir):
    calls = []
    script = [
        lambda r, u: CONTRACT_OK,
        lambda r, u: RESEARCH_OK,
        lambda r, u: STRATEGY_OK,
        lambda r, u: PLAN_OK,           # v1
        lambda r, u: REVIEW_REJECT,
        lambda r, u: PLAN_OK,           # v2
        lambda r, u: REVIEW_REJECT,
        lambda r, u: PLAN_OK,           # v3 (max_revisions=2 -> third draft allowed)
        lambda r, u: REVIEW_REJECT,
    ]
    store = FileArtifactStore(str(tmpdir))
    planner = MultiStagePlanner(config=Cfg(), run_stage=make_runner(script, calls), store=store)
    result = run(planner.plan(task="x"))
    assert result["status"] == "needs_revision" and result["stage"] == "review"
    review_calls = [c for c in calls if c["role"] == "reviewer"]
    assert len(review_calls) == 3


# ---------------- HermesSubagentRunner wiring (offline, delegate_task faked) ----------------

from agent.planning_multistage import HermesSubagentRunner

import tools.delegate_tool as dt
import hermes_cli.model_switch as ms


class Parent:
    provider = "openai"


def test_runner_passes_stage_prompt_and_resolved_alias(monkeypatch):
    captured = {}

    def fake_delegate(**kwargs):
        captured.update(kwargs)
        return json.dumps({"results": [{"status": "completed", "summary": '{"ok": true}'}]},
                          ensure_ascii=False)

    monkeypatch.setattr(dt, "delegate_task", fake_delegate)
    monkeypatch.setattr(ms, "resolve_alias", lambda a, current_provider="":
                        ("openai", "gpt-resolved", a) if a == "heavy" else None)

    parent = Parent()
    runner = HermesSubagentRunner(parent_agent=parent, aliases={"planner": "heavy"})
    out = runner(role="planner", model_alias="heavy", system_prompt="SYS", user_prompt="USER-JSON")
    assert out == '{"ok": true}'
    assert "SYS" in captured["context"] and "USER-JSON" in captured["context"]
    assert captured["credentials_cfg"] == {"model": "gpt-resolved"}
    assert captured["parent_agent"] is parent


def test_runner_unknown_alias_passes_through_and_errors_surface(monkeypatch):
    def fake_delegate(**kw):
        return json.dumps({"error": "no route"}, ensure_ascii=False)

    monkeypatch.setattr(dt, "delegate_task", fake_delegate)
    monkeypatch.setattr(ms, "resolve_alias", lambda a, current_provider="": None)
    runner = HermesSubagentRunner(parent_agent=Parent(), aliases={})
    with pytest.raises(RuntimeError, match="no route"):
        runner(role="reviewer", model_alias="review-alias",
               system_prompt="S", user_prompt="U")


def test_runner_child_failure_and_empty_summary(monkeypatch):
    cases = [
        json.dumps({"results": [{"status": "failed", "error": "rate_limit wall"}]}),
        json.dumps({"results": [{"status": "completed", "summary": "   "}]}),
        json.dumps({"results": []}),
    ]
    monkeypatch.setattr(ms, "resolve_alias", lambda a, current_provider="": None)
    for i, payload in enumerate(cases):
        monkeypatch.setattr(dt, "delegate_task", lambda **kw: payload)
        runner = HermesSubagentRunner(parent_agent=Parent(), aliases={})
        with pytest.raises(RuntimeError):
            runner(role="contract", model_alias="x", system_prompt="S", user_prompt="U")

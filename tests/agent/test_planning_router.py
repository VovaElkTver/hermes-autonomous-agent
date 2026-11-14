"""Delivery-3 P1a tests: planning.mode=auto routes trivial turns around the pipeline.

Router verdicts are code-only (no LLM): grey zone defaults to PLAN (anti-lazy bias,
Вл 14.11). Matrix: off/unknown modes behave as legacy; auto skips chatter with an
observable journal event; auto admits work requests into the conveyor.
"""

import json
import sys

sys.path.insert(0, "/home/qwen/hermes-agent")

from agent.planning_router import classify_route


def test_trivial_direct():
    for msg in ("который час?", "ок", "спасибо, принял", "ты тут?",
                "я сейчас файл кину", "+"):
        route, reason, conf = classify_route(msg)
        assert route == "direct", (msg, reason)
        assert 0 <= conf <= 1


def test_work_requests_plan():
    for msg in ("почини тесты в tests/agent/test_x.py и запушь",
                "перепиши agent/planning_coordinator.py: убрать гонку",
                "составь план: миграция state.db на новый формат",
                "надо починить баг, потом написать тест, затем задеплоить",
                "а добавь восстановление состояния после рестарта гейтвея ~/.hermes/config.yaml"):
        route, reason, _ = classify_route(msg)
        assert route == "plan", (msg, reason)


def test_explicit_plan_request_always_wins():
    route, _, conf = classify_route("составь план по пятипроходке")
    assert route == "plan" and conf == 1.0


def test_grey_zone_defaults_to_plan():
    route, reason, _ = classify_route("разберись с этим побольше")
    assert route == "plan" and reason == "grey_zone_default"

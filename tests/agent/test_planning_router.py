"""Delivery-3 P1a tests: planning.mode=auto routes trivial turns around the pipeline.

Router verdicts are code-only (no LLM). Bias policy Вл 15.11 (дубли ⎇-нот): the
grey zone requires a WORK SIGNAL — planning is admitted for explicit plan
requests, multi-path, imperative-with-path, multistep markers and long text; a
bare short imperative is a one-shot command and signal-free chat is discussion.
Quote prefixes are context and are stripped before classification.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

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


def test_grey_zone_requires_work_signal():
    # Signal-free remark: discussion, not a conveyor (was grey_zone_default->plan).
    route, reason, _ = classify_route("разберись с этим побольше как-нибудь")
    assert route == "direct" and reason == "grey_zone_no_work_signal", (route, reason)
    route, reason, _ = classify_route("интересная идея про слой состояний в целом")
    assert route == "direct" and reason == "grey_zone_no_work_signal", (route, reason)


def test_bare_imperative_is_one_shot_not_a_plan():
    # "мерж"/"смерж всё" — single command; live session executes it directly.
    route, reason, _ = classify_route("смерж")
    assert route == "direct" and reason == "one_shot_imperative", (route, reason)
    route, reason, _ = classify_route("мерж")
    assert route == "direct" and reason == "one_shot_imperative", (route, reason)


def test_quote_prefix_is_context_not_signal():
    quoted = ('[Replying to your previous message: "⎇ main: ✅ Background task complete\n'
              'Prompt: "проанализируй несколько последних сообщений, каждое из них с'
              '"\n\n**Вердикт: подтверждается — реального join не происходит.**\n' +
              "x" * 300 + '"]\n\nда')
    route, reason, _ = classify_route(quoted)
    assert route == "direct" and reason == "chatter_ack", (route, reason)


def test_long_imperative_sentence_still_plans():
    route, reason, _ = classify_route("проверь " + "состояние всех компонентов форка " * 5)
    assert route == "plan" and reason == "imperative_long", (route, reason)

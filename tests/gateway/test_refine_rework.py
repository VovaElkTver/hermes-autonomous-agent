"""Refine-rework v1: NO_CHANGE-first contract, structured plan, deterministic gate.

Covers the design doc ~/projects/outer-contour/refine-rework-design.md, first
iteration: the new DEFAULT_TEMPLATE, plan parsing/screening heuristics and the
NO_CHANGE / NEEDS_CONTEXT short-circuits in _analyze.
"""

import json

from gateway import refine_managed as rm
from gateway.refine_managed import parse_plan, screen_plan


# ------------------------------------------------------------------ template
def test_template_is_no_change_first_and_carries_slots():
    t = rm.DEFAULT_TEMPLATE
    assert "NO_CHANGE" in t
    assert "По умолчанию: NO_CHANGE" in t
    # gate sections from the owner's spec
    for section in ("НАЙДИ КАНДИДАТОВ", "ИНВАРИАНТ", "ОТСЕЙ НЕПРИГОДНОЕ",
                    "ТИП И ОБЛАСТЬ", "СРАВНИ С СУЩЕСТВУЮЩИМ", "ПОПРОБУЙ ОТКЛОНИТЬ",
                    "ФОРМАТ РЕЗУЛЬТАТА"):
        assert section in t, section
    # dedup-before-create is mandatory wording
    assert "dedup_checked_ids" in t
    # slots consumed by _compile_prompt survive
    for slot in ("{specialty_context}", "{material}", "{focus_block}"):
        assert slot in t, slot


def test_template_replaces_quota_style_prompting():
    t = rm.DEFAULT_TEMPLATE
    assert "Не нужно извлекать знание из каждого эпизода" in t
    # NO_CHANGE is named as an успешный результат, and rejection pass exists.
    assert "Отсутствие изменений — успешный результат" in t


# ------------------------------------------------------------------ parsing
def test_parse_plan_envelope():
    raw = 'pre\n```json\n{"status": "NO_CHANGE", "changes": [], "deferred": [], "rejected": []}\n```\n'
    status, changes = parse_plan(raw)
    assert status == "NO_CHANGE"
    assert changes == []


def test_parse_plan_legacy_list_degrades_to_propose():
    legacy = [{"kind": "memory", "topic": "_scratch_test", "content": "c", "rationale": "r"}]
    raw = "```json\n" + json.dumps(legacy) + "\n```"
    status, changes = parse_plan(raw)
    assert status == "PROPOSE_CHANGES"
    assert changes == legacy


def test_parse_plan_garbage_is_propose_empty():
    assert parse_plan("no json at all") == ("PROPOSE_CHANGES", [])
    assert parse_plan("```json\n{not json}\n```") == ("PROPOSE_CHANGES", [])


# ------------------------------------------------------------------ screening
def _env_change(**kw):
    base = {
        "candidate_id": "c1",
        "operation": "CREATE",
        "knowledge_kind": "PROCEDURE",
        "kind": "skill",
        "topic": "create",
        "proposed_content": "При X проверь Y; если Z — выполни A; результат подтверди B.",
        "future_decision_changed": "выбор между пересборкой и правкой конфига",
        "dedup_checked_ids": ["some-skill"],
    }
    base.update(kw)
    return base


def test_screen_defers_concrete_dates_hashes_in_procedure():
    ch = _env_change(proposed_content="12.11.2026 после коммита abc1234beef проверь настройки")
    cards, deferred, rejected = screen_plan([ch])
    assert cards == [] and not rejected
    assert len(deferred) == 1
    assert "конкретика" in deferred[0]["reason"]


def test_screen_passes_invariant_procedure():
    cards, deferred, rejected = screen_plan([_env_change()])
    assert len(cards) == 1 and not deferred and not rejected
    card = cards[0]
    assert card["kind"] == "skill" and card["action"] == "create"


def test_screen_create_without_dedup_is_rejected():
    ch = _env_change(dedup_checked_ids=[])
    cards, deferred, rejected = screen_plan([ch])
    assert cards == [] and deferred == []
    assert len(rejected) == 1 and "CREATE" in rejected[0]["reason"]


def test_screen_fact_with_details_is_not_punished(monkeypatch):
    monkeypatch.setattr(rm, "_registry_rule", lambda name: {"topic": name})
    ch = _env_change(knowledge_kind="FACT", kind="memory", topic="_scratch_test",
                     proposed_content="шлюз слушает на 192.168.9.100:8012")
    cards, deferred, rejected = screen_plan([ch])
    assert len(cards) == 1 and not deferred and not rejected


def test_screen_legacy_card_passthrough(monkeypatch):
    monkeypatch.setattr(rm, "_registry_rule", lambda name: {"topic": name})
    legacy = {"kind": "memory", "topic": "_scratch_test", "content": "pc меняет поведение",
              "rationale": "нужная тема"}
    cards, deferred, rejected = screen_plan([legacy])
    assert len(cards) == 1 and not deferred and not rejected
    assert cards[0]["content"] == "pc меняет поведение"


# ------------------------------------------------------------------ _analyze
def _mk_run(tmp_path, key="k"):
    return rm.ManagedRun(run_id=key, chat_key=key, source="manual", focus=None,
                         messages_snapshot=[{"role": "user", "content": "m"}])


def test_analyze_no_change_short_circuit(tmp_path, monkeypatch):
    emitted = []
    run = _mk_run(tmp_path, "k1")
    monkeypatch.setattr(rm, "_emit", lambda r, m: emitted.append(m))
    monkeypatch.setattr(rm, "_run_analysis_fork",
                        lambda agent, r, p: '```json\n{"status": "NO_CHANGE", "changes": []}\n```')
    rm._analyze(run)
    assert run.stage == "FINISHED"
    assert "NO_CHANGE" in emitted[-1]


def test_analyze_needs_context_short_circuit(tmp_path, monkeypatch):
    emitted = []
    run = _mk_run(tmp_path, "k2")
    monkeypatch.setattr(rm, "_emit", lambda r, m: emitted.append(m))
    monkeypatch.setattr(rm, "_run_analysis_fork",
                        lambda agent, r, p: '```json\n{"status": "NEEDS_CONTEXT", "changes": []}\n```')
    rm._analyze(run)
    assert run.stage == "NEEDS_CONTEXT"

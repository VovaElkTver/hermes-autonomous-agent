"""Declaration-gate contract tests (2026-11-03 incident).

Positive: the incident reply "повторный append-join живьём: строки перенесены
ровно один раз ..." declared success with no verifiable artifact and got a FAIL
verdict from the critic an hour later. Negative: the same reply WITH the artifact
citations it should have had passes untouched.
"""

import importlib

from gateway.declaration_gate import MARKER, annotate_unverified_declaration

gate = importlib.import_module("gateway.declaration_gate")


def test_incident_reply_gets_marked():
    reply = (
        "Проверка. Повторный append-join живьём: строки перенесены ровно один раз, "
        "ребёнок отстыкован, watermark освобождён."
    )
    out = annotate_unverified_declaration(reply)
    assert out.endswith(MARKER)
    assert out.startswith(reply)


def test_reply_with_artifacts_passes_untouched():
    reply = (
        "Проверка. test_session_fork_branches 18/18, остальные fork-наборы — 170 зелёных. "
        "Закоммичено: d5810f700e (правка+тест), journal задача в 70e2bb8704."
    )
    assert annotate_unverified_declaration(reply) == reply


def test_reply_with_test_path_artifact_passes():
    reply = "Сделано: тесты в tests/gateway/test_media_pin.py зелёные."
    assert annotate_unverified_declaration(reply) == reply


def test_short_commit_hash_counts_as_artifact():
    # Real short-hash length on this repo is 10 chars (git log --oneline).
    reply = "Закоммичено: d5810f700e (правка+тест), готово."
    assert annotate_unverified_declaration(reply) == reply


def test_plain_text_without_success_words_untouched():
    reply = "Уточни, пожалуйста, какой из двух вариантов выбрать."
    assert annotate_unverified_declaration(reply) == reply


def test_short_response_below_min_length_not_marked():
    assert annotate_unverified_declaration("Готово.") == "Готово."


def test_off_switch(monkeypatch):
    monkeypatch.setenv("HERMES_DECLARATION_GATE", "0")
    reply = "Всё исправлено и перенесено ровно один раз."
    assert gate.annotate_unverified_declaration(reply) == reply

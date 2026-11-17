"""Claim-verify stop gate: phantom paths and completed-effect claims without
in-turn evidence (agent/claim_verify.py + wiring in agent/turn_stop_gates.py)."""
import json
import os
from types import SimpleNamespace

import pytest

from agent import claim_verify as cv

from agent.claim_verify import _phantom_paths, _unsupported_claims, build_claim_nudge


def _agent(**kw):
    base = dict(_turn_file_mutation_paths=set(), _claim_verify_nudges=0)
    base.update(kw)
    return SimpleNamespace(**base)


def _msgs(user_text="сделай", calls=()):
    m = [{"role": "user", "content": user_text}]
    if calls:
        m.append({"role": "assistant", "content": None,
                  "tool_calls": [{"function": {"name": n, "arguments": a}} for n, a in calls]})
    return m


TERM = ("terminal", json.dumps({"command": "git push origin main"}))


class TestPhantomPaths:
    def test_missing_path_in_existing_dir_flagged(self, tmp_path, monkeypatch):
        answer = f"Готово, конфиг лежит в {tmp_path}/cron-jobs.json — там все пиновки."
        assert _phantom_paths(answer, _agent()) == [f"{tmp_path}/cron-jobs.json"]

    def test_existing_path_passes(self, tmp_path):
        f = tmp_path / "real.txt"
        f.write_text("x")
        assert _phantom_paths(f"Файл {f} прочитан полностью.", _agent()) == []

    def test_negated_mention_passes(self, tmp_path):
        answer = f"Файла {tmp_path}/cron-jobs.json не нашёл — его никогда не было."
        assert _phantom_paths(answer, _agent()) == []

    def test_unresolvable_parent_passes(self):
        # URL/API path shapes must never fire (endpoint docs, matrix paths).
        answer = ("Дёрнул /_matrix/client/v1/messages и /_synapse/admin/v1/server_version, "
                  "оба вернули ок, коннектор живой.")
        assert _phantom_paths(answer, _agent()) == []

    def test_path_written_this_turn_passes(self, tmp_path):
        token = f"{tmp_path}/brand-new.md"
        agent = _agent(_turn_file_mutation_paths={token})
        assert _phantom_paths(f"Записал заметку {token}.", agent) == []


class TestEnabledFlag:
    def test_config_token_off(self):
        assert cv.claim_verify_enabled({"agent": {"claim_verify": "off"}}) is False

    def test_default_on(self):
        assert cv.claim_verify_enabled({"agent": {}}) is True


class TestEffectClaims:
    def test_merge_claim_without_git_command_flagged(self):
        answer = "Всё смержено в main и запушено в origin, можно тестировать активацию."
        assert _unsupported_claims(answer, "") == ["git"]

    def test_merge_claim_with_git_command_passes(self):
        answer = "Всё смержено в main и запушено, тесты зелёные."
        blob = json.dumps({"command": "git push origin elk/x"})
        assert _unsupported_claims(answer, blob) == []

    def test_restart_claim_needs_systemctl(self):
        answer = "Гейтвей перезапущен, юнит снова активен после рестарта."
        assert "service" in _unsupported_claims(answer, "ls -la")
        assert "service" not in _unsupported_claims(
            answer, "systemctl --user restart hermes-gateway")

    def test_plain_answer_no_claims(self):
        assert _unsupported_claims("Разобрал по журналам, кнопки было две.", "") == []


class TestNudge:
    def test_builds_once_and_budget_respected(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cv, "claim_verify_enabled", lambda *a: True)
        answer = (f"Готово: смержено в origin/main, сводка в {tmp_path}/verdict.md — "
                  f"файл лежит, всё чисто, активация не требуется.")
        nudge = build_claim_nudge(_agent(), answer, _msgs())
        assert nudge and "claim-verify" in nudge
        assert "verdict.md" in nudge and "git" in nudge
        agent = _agent(_claim_verify_nudges=3)
        assert build_claim_nudge(agent, answer, _msgs()) is None

    def test_disabled_via_env(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cv, "claim_verify_enabled", lambda *a: False)
        assert build_claim_nudge(_agent(), "Всё смержено и запушено, готово.", _msgs()) is None

    def test_turn_boundary_ignores_previous_turn_evidence(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cv, "claim_verify_enabled", lambda *a: True)
        # git push happened in the PREVIOUS turn -> new turn's claim is unsupported
        msgs = _msgs(calls=[TERM]) + [
            {"role": "assistant", "content": "запушил"},
            {"role": "user", "content": "а в origin есть?"},
        ]
        answer = "Да, всё уже смержено и запушено в origin ещё вчера, я проверял ранее." # 40+ chars gate
        assert build_claim_nudge(_agent(), answer, msgs) is not None

    def test_short_answers_never_nudged(self, monkeypatch):
        monkeypatch.setattr(cv, "claim_verify_enabled", lambda *a: True)
        assert build_claim_nudge(_agent(), "Смержено.", _msgs()) is None


class TestGateWiring:
    def _stub(self, monkeypatch):
        import agent.turn_stop_gates as tsg

        for name in ("_late_lsp_verdicts", "_verify_on_stop_nudge", "_pre_verify_nudge"):
            monkeypatch.setattr(tsg, name, lambda *a, **k: None)
        if hasattr(tsg, "_kanban_stop_nudge"):
            monkeypatch.setattr(tsg, "_kanban_stop_nudge", lambda *a, **k: None)
        monkeypatch.setattr(tsg.investigation_hooks, "stop_nudge", lambda *a, **k: None)
        return SimpleNamespace(
            _session_messages=None, _interim_content_was_streamed=lambda text: False,
            _flush_messages_to_session_db=lambda *a, **k: None,
            _emit_interim_assistant_message=lambda *a, **k: None,
            _turn_file_mutation_paths=set(), _claim_verify_nudges=0,
        )

    def _run(self, stub, messages, answer):
        import agent.turn_stop_gates as tsg

        final_msg = {"role": "assistant", "content": answer}
        return tsg.apply_stop_gates(
            stub, final_msg, final_response=answer, messages=messages,
            conversation_history=None, pending_verification_response=None,
            pending_verification_response_previewed=None), final_msg

    def test_gate_fires_once_then_quiet(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cv, "claim_verify_enabled", lambda *a: True)
        stub = self._stub(monkeypatch)
        answer = (f"Работа завершена: смержено в main, итог в {tmp_path}/report.md, "
                  f"проверки не требуются.")
        messages = _msgs()
        v1, fm1 = self._run(stub, messages, answer)
        assert v1.continue_turn is True
        assert fm1["finish_reason"] == "claims_unverified"
        nudge = messages[-1]
        assert nudge["role"] == "user" and nudge["_claim_verify_synthetic"] is True
        v2, _ = self._run(stub, messages, answer)
        assert v2.continue_turn is False

    def test_clean_answer_passes(self, monkeypatch):
        monkeypatch.setattr(cv, "claim_verify_enabled", lambda *a: True)
        stub = self._stub(monkeypatch)
        answer = "Посмотрел журнал: кнопка suspend была нажата в 21:13:57, вот строки."
        v, _fm = self._run(stub, _msgs(), answer)
        assert v.continue_turn is False

"""managed_refine — приёмочные тесты (ТЗ «Управляемый анализ» §6 в редакции Вл 10.11).

Правка Вл 10.11: перехватывается ВЕСЬ бэкграунд-рефайн (вариант А), гейт владельца
живёт максимум REFINE_GATE_TIMEOUT_S, тишина => намерения записи дописываются в
need2write.md штатным record_need2write. ТЗ-номера сохранены; T8 переформулирован
под новый скоуп, добавлен T9 (обратная совместимость флага).
"""

from __future__ import annotations

import json
import time

import pytest

import gateway.refine_managed as rm


@pytest.fixture()
def fake_cfg(monkeypatch):
    holder = {"map": {}}
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: holder["map"], raising=False)
    return holder["map"]


@pytest.fixture()
def tmp_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture()
def fast_gate(monkeypatch):
    monkeypatch.setattr(rm, "REFINE_GATE_TIMEOUT_S", 0.25)


class StubAgent:
    def __init__(self, key="platform-x:chat-1"):
        self.session_chat_id = key
        self.published = []
        self.background_review_callback = self._publish

    def _publish(self, message):
        self.published.append(str(message))


def wait_until(predicate, timeout_s: float = 5.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def drain(agent, key: str) -> None:
    """Дождаться завершения прогона (RUNS опустел) и схлопнуть потоки."""
    wait_until(lambda: key not in rm.RUNS)


def discuss_ready(key: str) -> bool:
    """Гейт DISCUSS зарегистрирован и открыт."""
    return bool(rm.RUNS.get(key) and rm.RUNS[key].stage == "DISCUSS"
                and key in rm._GATES and rm._GATES[key].verdict is None)


@pytest.fixture(autouse=True)
def clean_state():
    rm.RUNS.clear()
    rm._GATES.clear()
    yield
    rm.RUNS.clear()
    rm._GATES.clear()


CARDS_RAW = (
    "Some prose.\n```json\n"
    '[{"kind": "memory", "topic": "elk-topic", "action": "add",'
    ' "content": "урок про управляемый рефайн", "rationale": "тест"}]\n'
    "```\ntrailer"
)


# ---------------------------------------------------------------- T1

def test_t1_prepare_stops_before_fork_and_persists_material(fake_cfg, tmp_home, fast_gate):
    fake_cfg["managed_refine"] = True
    agent = StubAgent()
    consumed = rm.intercept_spawn(
        agent, messages_snapshot=[{"role": "user", "content": "ход диалога про тему"},
                                  {"role": "assistant", "content": "ответ"}],
        review_memory=True, explicit=True)
    assert consumed is True
    runs = list((tmp_home / "refine_runs").iterdir())
    assert len(runs) == 1
    run_dir = runs[0]
    assert "ход диалога" in (run_dir / "digest.txt").read_text(encoding="utf-8")
    assert (run_dir / "prompt_used.txt").read_text(encoding="utf-8")
    joined = "\n".join(agent.published)
    assert "ход диалога" in joined and "prompt_used.txt" in joined
    # владелец молчит -> таймаут -> отложка, форк по-прежнему не стартовал
    assert wait_until(lambda: agent.session_chat_id not in rm.RUNS)
    need2write = (tmp_home / "need2write.md").read_text(encoding="utf-8")
    assert "refine-request" in need2write


# ---------------------------------------------------------------- T2 + T7

def test_t2_prompt_correction_reaches_analysis(fake_cfg, tmp_home, fast_gate, monkeypatch):
    fake_cfg["managed_refine"] = True
    fake_cfg["specialty_context"] = ""
    agent = StubAgent()
    assert rm.intercept_spawn(agent, messages_snapshot=[{"role": "user", "content": "материал"}],
                              review_memory=True, explicit=True) is True
    captured = {}

    def _fake_fork(parent, run, final_prompt):
        captured["final_prompt"] = final_prompt
        return CARDS_RAW

    monkeypatch.setattr(rm, "_run_analysis_fork", _fake_fork)
    rm.handle_command(agent.session_chat_id, "переписанный промпт с моим фокусом", "")
    drain(agent, agent.session_chat_id)
    run_id = captured["final_prompt"]
    assert "переписанный промпт с моим фокусом" in run_id
    # поправка реально записана в prompt_used.txt
    state_dir = next((tmp_home / "refine_runs").iterdir())
    assert "переписанный промпт" in (state_dir / "prompt_used.txt").read_text(encoding="utf-8")


def test_t7_specialty_slot_disappears_when_empty(fake_cfg, tmp_home):
    fake_cfg["specialty_context"] = ""
    run = rm.ManagedRun(run_id="r", chat_key="k", source="manual", focus=None,
                        messages_snapshot=[{"role": "user", "content": "m (elk-topic)"}])
    prompt = rm._compile_prompt(run, None)
    assert "Specialty context" not in prompt
    fake_cfg["specialty_context"] = "инженерия полётов"
    prompt2 = rm._compile_prompt(run, None)
    assert "инженерия полётов" in prompt2 and "{specialty_context}" not in prompt2


# ---------------------------------------------------------------- T4 (validation gates PROPOSE)

def _seed_topic_registry(tmp_home, tmp_path):
    reg = tmp_home / "memories" / "topics.yaml"
    reg.parent.mkdir(parents=True, exist_ok=True)
    store = tmp_path / "topics" / "elk-topic.md"
    reg.write_text(
        "topics:\n"
        "  - name: elk-topic\n"
        "    match: [\"elk-topic\"]\n"
        f"    store: {store}\n"
        "    pointer: «Тема elk-topic — подробно в её хранилище.»\n",
        encoding="utf-8")
    return store


def test_t4_invalid_topic_rejected_valid_passes_to_propose(fake_cfg, tmp_home, fast_gate, monkeypatch):
    fake_cfg["managed_refine"] = True
    _seed_topic_registry(tmp_home, tmp_home)
    agent = StubAgent()
    raw_bad = ('```json\n[{"kind": "memory", "topic": "нет-такой-темы", "action": "add",'
               ' "content": "x"}]\n```')
    assert rm.intercept_spawn(agent, messages_snapshot=[{"role": "user", "content": "m (elk-topic)"}],
                              review_memory=True, explicit=True) is True
    monkeypatch.setattr(rm, "_run_analysis_fork", lambda *a, **k: raw_bad)
    rm.handle_command(agent.session_chat_id, "го промпт", "")
    drain(agent, agent.session_chat_id)
    joined = "\n".join(agent.published)
    assert "нет-такой-темы" in joined and "not in the registry" in joined
    # прогон без единой валидной карточки падает, не оставляя висящих RUNS
    assert agent.session_chat_id not in rm.RUNS

    # валидная карточка доходит до DISCUSS и публикуется
    agent2 = StubAgent(key="platform-x:chat-2")
    monkeypatch.setattr(rm, "_run_analysis_fork", lambda *a, **k: CARDS_RAW)
    assert rm.intercept_spawn(agent2, messages_snapshot=[{"role": "user", "content": "m (elk-topic)"}],
                              review_memory=True, explicit=True) is True
    rm.handle_command(agent2.session_chat_id, "го", "")
    assert wait_until(lambda: any("CANDIDATES" in p for p in agent2.published))
    assert wait_until(lambda: discuss_ready(agent2.session_chat_id))
    run2 = rm.RUNS.get(agent2.session_chat_id)
    assert run2 is not None and run2.stage == "DISCUSS"


# ---------------------------------------------------------------- T3 + T6

def test_t3_defer_at_discuss_then_resume_applies_without_rerun(
        fake_cfg, tmp_home, fast_gate, monkeypatch):
    fake_cfg["managed_refine"] = True
    store_path = _seed_topic_registry(tmp_home, tmp_home)
    agent = StubAgent()
    assert rm.intercept_spawn(agent, messages_snapshot=[{"role": "user", "content": "m (elk-topic)"}],
                              review_memory=True, explicit=True) is True
    fork_calls = {"n": 0}

    def _fork(*a, **k):
        fork_calls["n"] += 1
        return CARDS_RAW

    monkeypatch.setattr(rm, "_run_analysis_fork", _fork)
    rm.handle_command(agent.session_chat_id, "го", "")
    assert wait_until(lambda: any("CANDIDATES" in p for p in agent.published))
    assert wait_until(lambda: discuss_ready(agent.session_chat_id))
    state_dir = next((tmp_home / "refine_runs").iterdir())
    raw_before = (state_dir / "response_raw.txt").read_bytes()

    # тишина на DISCUSS -> карточки уезжают в need2write с run_id
    assert wait_until(lambda: agent.session_chat_id not in rm.RUNS)
    n2w = (tmp_home / "need2write.md").read_text(encoding="utf-8")
    assert "card-1-memory" in n2w and state_dir.name in n2w
    deferred_state = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
    assert deferred_state["stage"].startswith("DEFERRED")

    # resume: продолжение с той же карточкой, БЕЗ повторного анализа (response_raw не переписан)
    reply = rm.handle_command(agent.session_chat_id, "resume", state_dir.name, agent=agent)
    assert "возобновлён" in reply
    wait_until(lambda: any("CANDIDATES" in p for p in agent.published))
    assert rm.handle_command(agent.session_chat_id, "approve", "") is None
    assert wait_until(lambda: store_path.exists()), "APPLY должен создать файл темы"
    assert "урок про управляемый рефайн" in store_path.read_text(encoding="utf-8")
    assert (state_dir / "response_raw.txt").read_bytes() == raw_before
    assert fork_calls["n"] == 1, "resume не должен перезапускать ANALYZE"


# ---------------------------------------------------------------- T5 + T6

def test_t5_apply_under_administrator_mode_redirects_with_run_id(
        fake_cfg, tmp_home, fast_gate, monkeypatch):
    fake_cfg.update({"managed_refine": True, "administrator_mode": True})
    _seed_topic_registry(tmp_home, tmp_home)
    agent = StubAgent()
    assert rm.intercept_spawn(agent, messages_snapshot=[{"role": "user", "content": "m (elk-topic)"}],
                              review_memory=True, explicit=True) is True
    monkeypatch.setattr(rm, "_run_analysis_fork", lambda *a, **k: CARDS_RAW)
    rm.handle_command(agent.session_chat_id, "го", "")
    assert wait_until(lambda: any("CANDIDATES" in p for p in agent.published))
    assert wait_until(lambda: discuss_ready(agent.session_chat_id))
    rm.handle_command(agent.session_chat_id, "approve", "")
    drain(agent, agent.session_chat_id)

    assert not (tmp_home / "memories" / "MEMORY.md").exists(), "frozen write затронул хранилище"
    n2w = (tmp_home / "need2write.md").read_text(encoding="utf-8")
    state_dir = next((tmp_home / "refine_runs").iterdir())
    assert f"run={state_dir.name}" in n2w and "kind=memory" in n2w
    applied = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
    assert applied["stage"] == "APPLIED"


# ---------------------------------------------------------------- T8 (новая формулировка) + T9

def test_t8_auto_nudge_intercepted_timeout_records_need2write_without_fork(
        fake_cfg, tmp_home, fast_gate):
    fake_cfg["managed_refine"] = True
    agent = StubAgent()
    consumed = rm.intercept_spawn(
        agent, messages_snapshot=[{"role": "user", "content": "auto-материал"}],
        review_memory=True, explicit=False)
    assert consumed is True
    run = rm.RUNS.get(agent.session_chat_id)
    assert run is not None and run.source == "auto"
    assert wait_until(lambda: agent.session_chat_id not in rm.RUNS)
    n2w = (tmp_home / "need2write.md").read_text(encoding="utf-8")
    assert "refine-request" in n2w and "source=auto" in n2w
    joined = "\n".join(agent.published)
    assert "PREPARE (auto)" in joined


def test_t9_flag_off_keeps_upstream_atomic_behavior(fake_cfg, tmp_home):
    fake_cfg["managed_refine"] = False
    agent = StubAgent()
    assert rm.intercept_spawn(agent, messages_snapshot=[], review_memory=True, explicit=False) is False
    assert not (tmp_home / "refine_runs").exists()


# ---------------------------------------------------------------- command surface smoke

def test_status_and_stop_surface(fake_cfg, tmp_home, fast_gate):
    fake_cfg["managed_refine"] = True
    agent = StubAgent()
    assert rm.intercept_spawn(agent, messages_snapshot=[{"role": "user", "content": "m (elk-topic)"}],
                              review_memory=True, explicit=True) is True
    status = rm.handle_command(agent.session_chat_id, "status", "")
    assert "stage=PREPARE" in status
    assert rm.handle_command(agent.session_chat_id, "stop", "") is None
    drain(agent, agent.session_chat_id)
    assert "остановлен" in "\n".join(agent.published)

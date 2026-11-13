"""Topic router v1: /refine <name> — классификация темы материала прогона.

Contract: детерминированная оценка против ~/.hermes/memories/topics.yaml;
NEW/AMBIGUOUS останавливает прогон в TOPIC_CONFIRM до явной команды владельца
(/refine topic-new|topic-use). Смотри тест-матрицу: assess, extract, гейт
в _finalize_analysis, команды маршрутизации.
"""

import json

from gateway import refine_managed as rm


# ------------------------------------------------------------------ extract
def test_extract_topic_from_envelope():
    raw = '```json\n{"status": "PROPOSE_CHANGES", "changes": [], ' \
          '"topic": {"proposed_name": "cad", "matched": ["hermes-agency"], "evidence": "e"}}\n```'
    tp = rm.extract_topic(raw)
    assert tp["proposed_name"] == "cad"
    assert tp["matched"] == ["hermes-agency"]


def test_extract_topic_tolerates_garbage():
    assert rm.extract_topic("no json") is None
    assert rm.extract_topic('{"status": "NO_CHANGE"}') is None


# ------------------------------------------------------------------ assess
def _write_registry(tmp_path, topics):
    reg = tmp_path / "memories" / "topics.yaml"
    reg.parent.mkdir(parents=True, exist_ok=True)
    rows = ["topics:"]
    for name, match in topics:
        rows.append(f"  - name: {name}")
        rows.append("    match:")
        rows += [f"      - '{m}'" for m in match]
        rows.append(f"    store: '{tmp_path}/{name}.md'")
        rows.append(f"    pointer: 'Канон темы {name}'")
    reg.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return reg


def test_assess_single_deterministic_hit(tmp_path):
    reg = _write_registry(tmp_path, [("alpha", ["re:V-O\\d+"]), ("beta", ["cad-studio"])])
    ta = rm.assess_topic("разбор V-O9 и хвостов", registry_path=reg)
    assert ta["status"] == "EXISTING"
    assert ta["matched"] == ["alpha"]
    # модельный matched вне реестра не ломает дерминистику
    ta2 = rm.assess_topic("тема cad-studio почти готова", {"matched": ["nope"]},
                          registry_path=reg)
    assert ta2["status"] == "EXISTING" and ta2["matched"] == ["beta"]


def test_assess_ambiguous_and_model_resolution(tmp_path):
    reg = _write_registry(tmp_path, [("t1", ["foo"]), ("t2", ["bar"])])
    text = "материал и про foo, и про bar одновременно"
    ta = rm.assess_topic(text, registry_path=reg)
    assert ta["status"] == "AMBIGUOUS" and set(ta["candidates"]) == {"t1", "t2"}
    # модель разрешила неоднозначность одним именем
    ta2 = rm.assess_topic(text, {"matched": ["t2"]}, registry_path=reg)
    assert ta2["status"] == "EXISTING" and ta2["matched"] == ["t2"]


def test_assess_new_with_proposed_fallback_to_focus(tmp_path):
    reg = _write_registry(tmp_path, [("known", ["known_kw"])])
    ta = rm.assess_topic("совершенно новый сюжет zoo", {"proposed_name": "zoo", "evidence": "x"},
                         registry_path=reg)
    assert ta["status"] == "NEW" and ta["proposed_name"] == "zoo"
    ta2 = rm.assess_topic("другой новый сюжет", registry_path=reg, hint="focus-name")
    assert ta2["status"] == "NEW" and ta2["proposed_name"] == "focus-name"


# ------------------------------------------------------------------ registry append
def test_registry_append_idempotent(tmp_path, monkeypatch):
    reg = _write_registry(tmp_path, [("exists", ["kw1"])])
    monkeypatch.setattr(rm, "_registry_path", lambda: reg)
    store = rm._registry_append("fresh-topic")
    text = reg.read_text(encoding="utf-8")
    assert "fresh-topic" in text and store.exists()
    n_before = text.count("- name:")
    rm._registry_append("fresh-topic")  # повтор не плодит строк
    assert reg.read_text(encoding="utf-8").count("- name:") == n_before
    from tools.memory_topic_gate import load_registry
    rules = {r.name: r for r in load_registry(reg)}
    assert "fresh-topic" in rules and rules["fresh-topic"].store == str(store)


# ------------------------------------------------------------------ gate in flow
def _await_leave_confirm(run, timeout=5.0):
    """handle_command продолжает обработку в фоновом потоке — ждём выхода из гейта."""
    import time as _t
    deadline = _t.time() + timeout
    while _t.time() < deadline:
        if run.stage != "TOPIC_CONFIRM":
            return True
        _t.sleep(0.02)
    return False


class _FakeGate:
    def __init__(self):
        self.event = __import__("threading").Event()
        self.verdict = None

    def resolve(self, verdict, payload=None):
        self.verdict = verdict


def _mk_run(tmp_path, monkeypatch, messages):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    run = rm.ManagedRun(run_id="r_test", chat_key="test:1", source="manual",
                        focus=None, messages_snapshot=messages,
                        review_memory=False, review_skills=False)
    run.dir.mkdir(parents=True, exist_ok=True)
    emitted = []
    monkeypatch.setattr(rm, "_emit", lambda run_, msg: emitted.append(msg))
    # unit-границы: обсуждение (блокирующий гейт владельца) вне сценария этих тестов
    monkeypatch.setattr(rm, "_wait_discuss", lambda run_, gate: None)
    return run, emitted


def test_finalize_new_topic_parks_run_in_topic_confirm(tmp_path, monkeypatch):
    _write_registry(tmp_path, [("agent-stuff", ["unrelated_kw"])])
    envelope = {"status": "PROPOSE_CHANGES",
                "changes": [{"candidate_id": "c1", "operation": "CREATE",
                             "kind": "memory", "topic": "brand-new-x",
                             "content": "правило", "dedup_checked_ids": ["d1"],
                             "novelty": "новое условие", "knowledge_kind": "PROCEDURE"}],
                "topic": {"proposed_name": "brand-new-x", "evidence": "нет совпадений"}}
    run, emitted = _mk_run(tmp_path, monkeypatch, [{"role": "user", "content": "msg про brand-new-x"}])
    rm._finalize_analysis(run, "```json\n" + json.dumps(envelope) + "\n```")
    assert run.stage == "TOPIC_CONFIRM"
    assert any("TOPIC_CONFIRM (NEW)" in m for m in emitted)
    assert run.topic_route is None
    # /refine topic-new достраивает реестр и доводит прогон до карточек
    rm.RUNS[run.chat_key] = run
    monkeypatch.setattr(rm, "_gate", lambda key: (_FakeGate(),)[0])
    try:
        reply = rm.handle_command(run.chat_key, "topic-new", "", agent=None)
        assert "brand-new-x" in reply and "продолжаю" in reply
        assert _await_leave_confirm(run)
        assert run.stage in ("DISCUSS", "PROPOSE")  # дожили вердикт до карточек
        assert run.cards[0]["topic"] == "brand-new-x"
        assert rm._registry_rule("brand-new-x") is not None  # запись реестра создана
    finally:
        rm.RUNS.pop(run.chat_key, None)


def test_finalize_ambiguous_requires_owner_choice(tmp_path, monkeypatch):
    _write_registry(tmp_path, [("left", ["левый"]), ("right", ["правый"])])
    envelope = {"status": "PROPOSE_CHANGES",
                "changes": [{"candidate_id": "c9", "operation": "UPDATE",
                             "kind": "memory", "topic": "?",
                             "knowledge_kind": "FACT", "proposed_content": "x"}]}
    run, emitted = _mk_run(tmp_path, monkeypatch, [{"role": "user", "content": "левый и правый"}])
    rm._finalize_analysis(run, "```json\n" + json.dumps(envelope) + "\n```")
    assert run.stage == "TOPIC_CONFIRM"
    assert any("AMBIGUOUS" in m for m in emitted)
    # выбор существующей темы: route без создания записи
    rm.RUNS[run.chat_key] = run
    monkeypatch.setattr(rm, "_gate", lambda key: (_FakeGate(),)[0])
    try:
        rm.handle_command(run.chat_key, "topic-use", "right", agent=None)
        assert _await_leave_confirm(run)
        assert run.topic_route == "right"
        assert run.cards and run.cards[0]["topic"] == "right"
    finally:
        rm.RUNS.pop(run.chat_key, None)


def test_existing_topic_bypasses_confirm(tmp_path, monkeypatch):
    _write_registry(tmp_path, [("known-t", ["аккуратный"])])
    envelope = {"status": "PROPOSE_CHANGES",
                "changes": [{"candidate_id": "c3", "operation": "CREATE",
                             "kind": "memory", "topic": "known-t", "content": "y"}]}
    run, emitted = _mk_run(tmp_path, monkeypatch, [{"role": "user", "content": "аккуратный материал"}])
    rm._finalize_analysis(run, "```json\n" + json.dumps(envelope) + "\n```")
    assert run.stage != "TOPIC_CONFIRM"
    assert not any("TOPIC_CONFIRM" in m for m in emitted)


def test_no_memory_cards_skips_topic_question(tmp_path, monkeypatch):
    _write_registry(tmp_path, [("a", ["a_kw"])])
    envelope = {"status": "PROPOSE_CHANGES",
                "changes": [{"candidate_id": "s1", "operation": "UPDATE",
                             "kind": "skill", "topic": "some-skill",
                             "action": "patch", "content": "z"}]}
    run, _emitted = _mk_run(tmp_path, monkeypatch, [{"role": "user", "content": "не про темы вообще"}])
    rm._finalize_analysis(run, "```json\n" + json.dumps(envelope) + "\n```")
    assert run.stage != "TOPIC_CONFIRM"


def test_command_guard_when_not_in_confirm(tmp_path, monkeypatch):
    reg = _write_registry(tmp_path, [("a", ["a_kw"])])
    monkeypatch.setattr(rm, "_registry_path", lambda: reg)
    run, _ = _mk_run(tmp_path, monkeypatch, [{"role": "user", "content": "x"}])
    run.stage = "PREPARE"
    rm.RUNS[run.chat_key] = run
    try:
        out = rm.handle_command(run.chat_key, "topic-use", "a", agent=None)
        assert "TOPIC_CONFIRM" in out
    finally:
        rm.RUNS.pop(run.chat_key, None)


# ------------------------- свободный ответ владельца (без синтаксиса команд) -------------------------

def test_owner_answer_existing_name_routes(tmp_path, monkeypatch):
    """«левый» в ответе = существующая тема, без topic-use."""
    _write_registry(tmp_path, [("left", ["левый"]), ("right", ["правый"])])
    run, emitted = _mk_run(tmp_path, monkeypatch, [{"role": "user", "content": "левый и правый"}])
    envelope = {"status": "PROPOSE_CHANGES",
                "changes": [{"candidate_id": "c9", "operation": "UPDATE",
                             "kind": "memory", "topic": "?",
                             "knowledge_kind": "FACT", "proposed_content": "x"}]}
    rm._finalize_analysis(run, "```json\n" + json.dumps(envelope) + "\n```")
    assert run.stage == "TOPIC_CONFIRM"
    rm.RUNS[run.chat_key] = run
    monkeypatch.setattr(rm, "_gate", lambda key: (_FakeGate(),)[0])
    try:
        reply = rm.handle_command(run.chat_key, "left", "", agent=None)
        assert _await_leave_confirm(run)
        assert run.topic_route == "left"
        assert run.cards and run.cards[0]["topic"] == "left"
    finally:
        rm.RUNS.pop(run.chat_key, None)


def test_owner_answer_new_name_creates(tmp_path, monkeypatch):
    """Незнакомое имя в ответе = создать тему с этим именем."""
    reg = _write_registry(tmp_path, [("known-t", ["аккуратный"])])
    envelope = {"status": "PROPOSE_CHANGES",
                "changes": [{"candidate_id": "c1", "operation": "CREATE",
                             "kind": "memory", "topic": "brand-new-x",
                             "knowledge_kind": "PROCEDURE",
                             "proposed_content": "правило",
                             "dedup_checked_ids": ["d1"], "novelty": "новое"}]}
    run, emitted = _mk_run(tmp_path, monkeypatch, [{"role": "user", "content": "нейтральный материал"}])
    envelope["changes"][0]["operation"] = "UPDATE"      # обходим dedup-гейт CREATE: фокус теста на маршруте темы
    rm._finalize_analysis(run, "```json\n" + json.dumps(envelope) + "\n```")
    assert run.stage == "TOPIC_CONFIRM"
    rm.RUNS[run.chat_key] = run
    monkeypatch.setattr(rm, "_gate", lambda key: (_FakeGate(),)[0])
    try:
        # заманить вопрос NEW любым статусом: имя "Fresh" отсутствует в реестре
        run.topic_assessment["status"] = "NEW"
        reply = rm.handle_command(run.chat_key, "fresh", "", agent=None)
        assert _await_leave_confirm(run)
        assert rm._registry_rule("fresh") is not None
        assert run.topic_route == "fresh"
        assert run.cards and run.cards[0]["topic"] == "fresh"
    finally:
        rm.RUNS.pop(run.chat_key, None)


def test_owner_answer_yes_uses_proposed_name(tmp_path, monkeypatch):
    reg = _write_registry(tmp_path, [("known-t", ["аккуратный"])])
    envelope = {"status": "PROPOSE_CHANGES",
                "changes": [{"candidate_id": "c1", "operation": "CREATE",
                             "kind": "memory", "topic": "brand-new-x",
                             "knowledge_kind": "PROCEDURE",
                             "proposed_content": "правило",
                             "dedup_checked_ids": ["d1"], "novelty": "новое"}]}
    run, emitted = _mk_run(tmp_path, monkeypatch, [{"role": "user", "content": "материал brand-new-x"}])
    rm._finalize_analysis(run, "```json\n" + json.dumps(envelope) + "\n```")
    assert run.stage == "TOPIC_CONFIRM"
    rm.RUNS[run.chat_key] = run
    monkeypatch.setattr(rm, "_gate", lambda key: (_FakeGate(),)[0])
    try:
        run.topic_assessment["status"] = "NEW"
        run.topic_assessment["proposed_name"] = "brand-new-x"
        rm.handle_command(run.chat_key, "да", "", agent=None)
        assert _await_leave_confirm(run)
        assert run.topic_route == "brand-new-x"
    finally:
        rm.RUNS.pop(run.chat_key, None)


def test_owner_answer_empty_and_unsure(tmp_path, monkeypatch):
    reg = _write_registry(tmp_path, [("known-t", ["аккуратный"])])
    envelope = {"status": "PROPOSE_CHANGES",
                "changes": [{"candidate_id": "c1", "operation": "CREATE",
                             "kind": "memory", "topic": "z",
                             "knowledge_kind": "FACT", "proposed_content": "x"}]}
    run, emitted = _mk_run(tmp_path, monkeypatch, [{"role": "user", "content": "неопределённый материал"}])
    rm._finalize_analysis(run, "```json\n" + json.dumps(envelope) + "\n```")
    assert run.stage == "TOPIC_CONFIRM"
    out = rm.handle_command(run.chat_key, "какая-то ерунда без темы?", "", agent=None)
    # пустой ответ без proposal → просьба имени, прогон не погиб
    assert run.stage == "TOPIC_CONFIRM" or "Назови тему" in str(out)

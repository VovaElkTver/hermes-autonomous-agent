"""memory.topic_gate — маршрутизация предметных записей из MEMORY.md в хранилища темы.

Приёмка ТЗ v0.1 (§8): кейсы 1–7 + контракт ответа §5.3 («записано в тему X»)
+ отказ смешанного батча без частичной записи.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tools.memory_tool_store import ENTRY_DELIMITER, MemoryStore

TOPICS_YAML = """\
topics:
  - name: hermes-agency
    match:
      - "~/projects/hermes-agency/**"
      - "re:^V-O\\\\d+"
      - "hermes-agency"
    store: "{store}"
    pointer: "Канон темы hermes-agency — {store}"
  - name: fleet
    match:
      - "дежурство флота"
    store: "{fleet_store}"
    pointer: "Канон темы fleet — {fleet_store}"
"""


@pytest.fixture()
def gate_env(tmp_path, monkeypatch):
    """Isolated MEMORY.md + topics.yaml; store with the gate ON."""
    reg = tmp_path / "topics.yaml"
    store = tmp_path / "hermes-agency-topic.md"
    fleet_store = tmp_path / "fleet-topic.md"
    reg.write_text(TOPICS_YAML.format(store=store, fleet_store=fleet_store),
                   encoding="utf-8")
    monkeypatch.setattr("tools.memory_tool.get_memory_dir",
                        lambda: tmp_path, raising=False)
    ms = MemoryStore(topic_gate=True, topic_registry=str(reg))
    ms.load_from_disk()
    env = type("Env", (), {})()
    env.ms, env.reg, env.store, env.fleet_store = ms, reg, store, fleet_store
    env.mem_path = tmp_path / "MEMORY.md"
    return env


def _entries(path):
    if not Path(path).exists():
        return []
    raw = Path(path).read_text(encoding="utf-8")
    return [e for e in (x.strip() for x in raw.split(ENTRY_DELIMITER)) if e]


# ── case 1: gate off / key missing → byte-for-byte legacy behaviour ────────

def test_gate_off_writes_globally(tmp_path, monkeypatch):
    monkeypatch.setattr("tools.memory_tool.get_memory_dir",
                        lambda: tmp_path, raising=False)
    ms = MemoryStore()  # no gate kwargs at all — «нет ключа = как сейчас»
    ms.load_from_disk()
    r = ms.add("memory", "политика личности")
    assert r["success"] is True
    assert _entries(tmp_path / "MEMORY.md") == ["политика личности"]
    assert not (tmp_path / "topics.yaml").exists()


# ── case 2: verdict mentioning the registered path → topic store ───────────

def test_matched_add_routes_to_topic(gate_env):
    e = gate_env
    r = e.ms.add("memory", "Вердикт V-O9 принят по ~/projects/hermes-agency/docs")
    assert r["success"] is True
    body = _entries(e.store)
    assert body == ["Вердикт V-O9 принят по ~/projects/hermes-agency/docs"]
    # MEMORY.md got ONLY the pointer line
    mem = _entries(e.mem_path)
    assert mem == [f"Канон темы hermes-agency — {e.store}"]


def test_response_reports_destination(gate_env):
    r = gate_env.ms.add("memory", "hermes-agency важен")
    assert r["success"] and r["topic"] == "hermes-agency"
    assert "hermes-agency-topic.md" in r["message"]


# ── case 3: political/user note unmatched → MEMORY.md as before ────────────

def test_unmatched_add_stays_global(gate_env):
    r = gate_env.ms.add("memory", "Дежурный по кухне Петя")
    assert r["success"] is True
    assert _entries(gate_env.mem_path) == ["Дежурный по кухне Петя"]
    assert not gate_env.store.exists()


def test_first_matching_topic_wins(gate_env):
    e = gate_env
    r = e.ms.add("memory", "дежурство флота в hermes-agency")
    assert r["topic"] == "hermes-agency"  # registry order, first wins
    assert _entries(e.store) == ["дежурство флота в hermes-agency"]
    assert not e.fleet_store.exists()


# ── case 4: repeated themed write → pointer not duplicated ─────────────────

def test_pointer_idempotent(gate_env):
    e = gate_env
    assert e.ms.add("memory", "hermes-agency запись раз")["success"]
    assert e.ms.add("memory", "hermes-agency запись два")["success"]
    pointers = [x for x in _entries(e.mem_path) if x.startswith("Канон темы")]
    assert len(pointers) == 1
    assert _entries(e.store) == ["hermes-agency запись раз", "hermes-agency запись два"]


# ── case 5: replace/remove over theme entries goes through the registry ────

def test_replace_and_remove_inside_topic(gate_env):
    e = gate_env
    e.ms.add("memory", "hermes-agency старая формулировка")
    r = e.ms.replace("memory", "hermes-agency старая формулировка", "hermes-agency новая")
    assert r["success"] is True
    assert _entries(e.store) == ["hermes-agency новая"]

    r = e.ms.remove("memory", "hermes-agency новая")
    assert r["success"] is True
    assert _entries(e.store) == []


def test_edit_missing_topic_entry_honest_error(gate_env):
    e = gate_env
    e.ms.add("memory", "затравка чтобы MEMORY.md существовал и без темы")  # unmatched seed
    r = e.ms.replace("memory", "V-O99 ничего такого нет", "x")
    assert r["success"] is False
    assert "hermes-agency" in r["error"]
    assert _entries(e.store) == []       # nothing written into topic store
    assert len(_entries(e.mem_path)) == 1  # global entry untouched by the failed edit


def test_edit_of_global_entry_untouched_by_gate(gate_env):
    e = gate_env
    e.ms.add("memory", "чисто личная политика")
    r = e.ms.patch("memory", "личная политика", "личная политика №2")
    assert r["success"] is True  # unmatched → legacy branch verbatim
    assert _entries(e.mem_path)[0].startswith("чисто личная политика")


def test_patch_inside_topic_entry(gate_env):
    e = gate_env
    e.ms.add("memory", "hermes-agency: срок stage-b — четверг")
    # probe = old_text, so the themed marker must sit inside the edited span
    r = e.ms.patch("memory", "hermes-agency: срок stage-b — четверг",
                   "hermes-agency: срок stage-b — пятницу")
    assert r["success"] is True
    assert _entries(e.store) == ["hermes-agency: срок stage-b — пятницу"]


# ── batch safety: no partial cross-store writes, ever ──────────────────────

def test_batch_with_topic_op_refused_atomically(gate_env):
    e = gate_env
    ops = [{"action": "remove", "old_text": "старое"},
           {"action": "add", "content": "hermes-agency вердикт"}]
    r = e.ms.apply_batch("memory", ops)
    assert r["success"] is False
    assert "hermes-agency" in r["error"]
    assert not e.store.exists()          # nothing was written anywhere
    assert _entries(e.mem_path) == []    # global side of the batch did NOT land


# ── registry resilience & live reload (need2write school) ──────────────────

def test_missing_registry_fails_open(gate_env, tmp_path):
    ms2 = MemoryStore(topic_gate=True,
                      topic_registry=str(tmp_path / "no-such.yaml"))
    ms2.load_from_disk()
    r = ms2.add("memory", "hermes-agency без реестра")
    assert r["success"] is True
    assert _entries(tmp_path / "MEMORY.md") == ["hermes-agency без реестра"]


def test_registry_reread_on_mtime(gate_env):
    e = gate_env
    e.reg.write_text(
        TOPICS_YAML.format(store=e.store, fleet_store=e.fleet_store)
        + '  - name: extra\n    match: ["золотой ключик"]\n'
        + f'    store: "{e.fleet_store}"\n'
        + '    pointer: "Канон темы extra"\n',
        encoding="utf-8")
    r = e.ms.add("memory", "нашёл золотой ключик")
    assert r["success"] and r["topic"] == "extra"


def test_exact_pointer_readd_is_noop(gate_env):
    e = gate_env
    ptr = f"Канон темы hermes-agency — {e.store}"
    r = e.ms.add("memory", ptr)
    assert r["success"] is True
    assert _entries(e.store) == []        # pointer went nowhere as an entry
    assert _entries(e.mem_path) == [ptr]


# ── case 6: diagnostics replay (audit), no auto-cleanup ────────────────────

def test_audit_classifies_manual_leak(gate_env):
    e = gate_env
    e.ms.add("memory", "политика личности — остаётся глобальной")
    e.mem_path.write_text(  # ручной мусор: проектный абзац вблокировали руками
        ENTRY_DELIMITER.join([f"Канон темы hermes-agency — {e.store}",
                              "вердикт hermes-agency от 05.11"]), encoding="utf-8")
    from tools.topic_gate_audit import classify
    leaks, pointers = classify(_entries(e.mem_path), e.ms.topic_registry)
    assert ("вердикт hermes-agency от 05.11", "hermes-agency") in leaks
    assert f"Канон темы hermes-agency — {e.store}" in pointers


# ── v0.2: registry snapshot injected into the system prompt ────────────────

def test_prompt_snapshot_gate_off_unchanged(tmp_path, monkeypatch):
    """Приёмка v0.2 кейс 1: gate off → промпт байт-в-байт как сейчас."""
    monkeypatch.setattr("tools.memory_tool.get_memory_dir",
                        lambda: tmp_path, raising=False)
    reg = tmp_path / "topics.yaml"
    reg.write_text(TOPICS_YAML.format(store=tmp_path / "st.md",
                                      fleet_store=tmp_path / "fl.md"), encoding="utf-8")
    (tmp_path / "MEMORY.md").write_text("старая запись", encoding="utf-8")
    ms_off = MemoryStore()
    ms_off.load_from_disk()
    off = ms_off.format_for_system_prompt("memory") or ""
    assert "authoritative over this snapshot" not in off
    assert "старая запись" in off
    # gate on adds ONLY the snapshot suffix on top of the identical base block
    ms_on = MemoryStore(topic_gate=True, topic_registry=str(reg))
    ms_on.load_from_disk()
    on = ms_on.format_for_system_prompt("memory") or ""
    assert on.startswith(off)
    assert "authoritative over this snapshot" in on[len(off):]


def test_prompt_snapshot_single_topic_block(tmp_path, monkeypatch):
    """Приёмка v0.2 кейс 2: одна тема → блок ≤5 строк (4 + дисклеймер)."""
    monkeypatch.setattr("tools.memory_tool.get_memory_dir",
                        lambda: tmp_path, raising=False)
    store = tmp_path / "hermes-agency-topic.md"
    reg = tmp_path / "topics.yaml"
    reg.write_text(TOPICS_YAML.format(store=store, fleet_store=tmp_path / "x.md").split("  - name: fleet")[0],
                   encoding="utf-8")
    ms = MemoryStore(topic_gate=True, topic_registry=str(reg))
    ms.load_from_disk()
    block = ms.format_for_system_prompt("memory") or ""
    lines = [ln for ln in block.splitlines() if ln.strip()]
    joined = "\n".join(lines)
    for frag in ("- hermes-agency",
                 "  match: ~/projects/hermes-agency/**, re:^V-O\\d+, hermes-agency"):
        assert frag in joined
    assert f"  store: {store}" in joined
    assert "  pointer: Канон темы hermes-agency" in joined
    # exactly: 4 topic lines + 1 disclaimer
    start = next(i for i, ln in enumerate(lines) if ln.startswith("- hermes-agency"))
    chunk = lines[start:]
    assert len(chunk) == 5
    assert chunk[-1].startswith("Registry may have changed")


def test_prompt_snapshot_absent_registry_no_block(tmp_path, monkeypatch):
    """gate on, реестра нет → слепок пуст, промпт как у гейта без реестра."""
    monkeypatch.setattr("tools.memory_tool.get_memory_dir",
                        lambda: tmp_path, raising=False)
    ms = MemoryStore(topic_gate=True, topic_registry=str(tmp_path / "no.yaml"))
    ms.load_from_disk()
    assert "authoritative over this snapshot" not in (ms.format_for_system_prompt("memory") or "")


def test_prompt_snapshot_multiline_fields_folded(tmp_path, monkeypatch):
    """Многосимвольные значения реестра не ломают построчную форму блока."""
    reg = tmp_path / "topics.yaml"
    store = tmp_path / "st.md"
    reg.write_text(
        'topics:\n'
        '  - name: "multi\\nline"\n'
        '    match: ["re:^X\\\\d+"]\n'
        f'    store: "{store}"\n'
        '    pointer: "p1\\np2"\n',
        encoding="utf-8")
    monkeypatch.setattr("tools.memory_tool.get_memory_dir",
                        lambda: tmp_path, raising=False)
    ms = MemoryStore(topic_gate=True, topic_registry=str(reg))
    ms.load_from_disk()
    block = ms.format_for_system_prompt("memory") or ""
    topic_lines = [ln for ln in block.splitlines() if ln.strip()]
    # name(1) + match(1) + store(1) + pointer(1) + disclaimer(1)
    assert "- multi line" in topic_lines
    assert "  pointer: p1 p2" in topic_lines


# ── visible probe answer (Вл 09.11): global add must REPORT the topic miss ──

def test_unmatched_global_add_reports_topic_probe(gate_env):
    resp = gate_env.ms.add("memory", "случайная заметка не про темы")
    assert resp["success"]
    probe = resp["topic_probe"]
    assert probe["matched"] is None
    assert "hermes-agency" in probe["topics_checked"]
    assert "registry" in probe["hint"]
    # the entry itself landed globally, byte-for-byte штатная ветка
    assert "случайная заметка не про темы" in _entries(gate_env.mem_path)


def test_matched_add_has_no_probe_field(gate_env):
    resp = gate_env.ms.add("memory", "hermes-agency: новое правило")
    assert resp["success"] and "topic_probe" not in resp
    assert resp["topic"] == "hermes-agency"


def test_gate_off_add_has_no_probe_field(tmp_path, monkeypatch):
    monkeypatch.setattr("tools.memory_tool.get_memory_dir",
                        lambda: tmp_path, raising=False)
    ms = MemoryStore()
    ms.load_from_disk()
    resp = ms.add("memory", "обычная запись")
    assert resp["success"] and "topic_probe" not in resp


def test_user_target_add_has_no_probe_field(gate_env):
    resp = gate_env.ms.add("user", "пользователь любит короткое")
    assert resp["success"] and "topic_probe" not in resp


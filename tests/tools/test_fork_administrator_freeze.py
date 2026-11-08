"""administrator_mode freeze + autonomous_mode flag semantics (Вл, 08.11).

autonomous_mode — только топ-левел config.yaml, default ON, без env.
administrator_mode — под-режим: КОДОВЫЙ запрет записи MEMORY.md/SKILL.md,
потребность дописывается в need2write.md."""

from __future__ import annotations

import json

import pytest
from pathlib import Path

import tools.fork_mode as fm


@pytest.fixture()
def fake_cfg(monkeypatch):
    holder = {"map": {}}
    monkeypatch.setattr("hermes_cli.config.load_config",
                        lambda: holder["map"], raising=False)
    return holder["map"]


@pytest.fixture()
def tmp_home(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


def test_autonomous_mode_defaults_on_without_config(fake_cfg) -> None:
    assert fm.autonomous_mode_enabled() is True


def test_env_override_is_ignored_by_design(fake_cfg, monkeypatch) -> None:
    fake_cfg["autonomous_mode"] = False
    monkeypatch.setenv("HERMES_ADMIN_MODE", "1")
    assert fm.autonomous_mode_enabled() is False


def test_administrator_mode_off_by_default(fake_cfg) -> None:
    assert fm.administrator_mode_enabled() is False


def test_memory_freeze_records_need2write_and_skips_store(
        fake_cfg, tmp_home, monkeypatch) -> None:
    from tools.memory_tool_store import MemoryStore
    store = MemoryStore()
    fake_cfg["administrator_mode"] = True

    result = store.add("memory", "факт про запрет записи")
    assert result["success"] is True and result["_frozen"] is True
    assert not (tmp_home / "memories" / "MEMORY.md").exists(), "frozen write touched the store"

    text = (tmp_home / "need2write.md").read_text(encoding="utf-8")
    assert "kind=memory" in text
    assert "факт про запрет записи" in text


def test_memory_unfrozen_write_still_lands(fake_cfg, tmp_home) -> None:
    from tools.memory_tool_store import MemoryStore
    store = MemoryStore()
    result = store.add("memory", "обычная запись")
    assert result.get("_frozen") is None and result["success"] is True
    assert "обычная запись" in (tmp_home / "memories" / "MEMORY.md").read_text(encoding="utf-8")


def test_skill_freeze_returns_success_shaped_record(fake_cfg, tmp_home) -> None:
    from tools.skill_manager_tool import skill_manage
    fake_cfg["administrator_mode"] = True
    raw = skill_manage(action="create", name="demo-skill", content="---\nname: demo-skill\n---\nbody")
    payload = json.loads(raw)
    assert payload["success"] is True and payload["_frozen"] is True
    recorded = Path(payload["recorded_to"])
    assert recorded.name == "need2write.md" and "demo-skill" in recorded.read_text(encoding="utf-8")


def test_skill_read_like_actions_are_not_frozen(fake_cfg, tmp_home) -> None:
    from tools.skill_manager_tool import skill_manage
    fake_cfg["administrator_mode"] = True
    raw = skill_manage(action="list", name="")
    payload = json.loads(raw)
    assert "_frozen" not in payload

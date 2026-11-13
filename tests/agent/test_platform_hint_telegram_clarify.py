"""The Telegram system prompt steers fixed-choice decisions to ``clarify`` (native inline buttons).

Without it the model faked decision controls with Telegram copy-to-clipboard buttons, which never
send a choice back to the waiting session. The advice is only given when the session's toolset has
``clarify``: scheduled jobs delivering to Telegram and clarify-disabled profiles must not be told to call it.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.system_prompt import build_system_prompt_parts


def _stable(platform, valid_tool_names):
    agent = SimpleNamespace(
        platform=platform, load_soul_identity=False, skip_context_files=False, valid_tool_names=valid_tool_names,
        _task_completion_guidance=False, _tool_use_enforcement=False, _environment_probe=False,
        _kanban_worker_guidance="", _memory_store=None, _memory_manager=None, _platform_hint_overrides={},
        model="", provider="", pass_session_id=False, session_id="")
    with (patch("agent.prompt_builder.load_soul_md", return_value=""),
          patch("agent.prompt_builder.build_environment_hints", return_value=""),
          patch("agent.prompt_builder.build_context_files_prompt", return_value="")):
        return build_system_prompt_parts(agent)["stable"]


def test_telegram_stable_prompt_prefers_clarify_buttons_over_fake_controls():
    stable = _stable("telegram", ["clarify", "terminal"])
    assert "You are on Telegram." in stable
    assert "call `clarify`" in stable and "tappable buttons" in stable
    assert "copy-to-clipboard" in stable


@pytest.mark.parametrize("surface", ["clarify_disabled_profile", "scheduled_delivering_to_telegram"])
def test_telegram_hint_never_recommends_clarify_without_the_tool(surface, monkeypatch):
    if surface == "scheduled_delivering_to_telegram":
        # A scheduled run delivering to Telegram sees the channel hint through the platform="cron"
        # delivery-destination seam; its toolset here carries no ``clarify``, so neither the channel
        # hint nor the destination echo may recommend calling it.
        monkeypatch.setenv("HERMES_SCHEDULED_AUTO_DELIVER_PLATFORM", "telegram")
        stable = _stable("cron", ["terminal", "read_file"])
        assert "Delivery destination (telegram): You are on Telegram." in stable
    else:
        stable = _stable("telegram", ["terminal", "read_file"])
        assert "You are on Telegram." in stable
    assert "clarify`" not in stable
    assert "copy-to-clipboard" not in stable

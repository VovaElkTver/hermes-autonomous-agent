"""Tests that instance identity (identity.name) flows into consumer surfaces.

Acceptance for the instance-identity step: on an instance whose config.yaml
sets ``identity.name``, that name must be visible in the A2A agent card.
The Matrix device_name path is asserted by ``scripts/verify_identity_e2e.py``
(it is inline in the login method, not a cheap unit target).
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from hermes_cli import identity


@pytest.fixture()
def tmp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    (tmp_path / "config.yaml").write_text(
        textwrap.dedent(
            """
            identity:
              name: rtx5090-worker
              id: "inst-rtx5090-001"
            """
        ),
        encoding="utf-8",
    )
    return tmp_path


def test_a2a_agent_card_uses_identity_name(tmp_home: Path) -> None:
    from plugins.platforms.a2a.adapter import _profile_agent_brand

    # The per-profile served agent card falls back to this brand when no
    # explicit ``name`` is configured.
    assert _profile_agent_brand("some-profile") == "rtx5090-worker"


def test_a2a_default_agent_name_prefers_identity(tmp_home: Path) -> None:
    from plugins.platforms.a2a.adapter import _default_agent_name

    assert _default_agent_name() == "rtx5090-worker"


def test_consumer_surfaces_fallback_to_legacy_when_unset() -> None:
    # No identity set -> consumers keep their legacy strings.
    assert identity.instance_label() is None
    from plugins.platforms.a2a.adapter import _profile_agent_brand

    assert _profile_agent_brand("some-profile") == "Hermes"

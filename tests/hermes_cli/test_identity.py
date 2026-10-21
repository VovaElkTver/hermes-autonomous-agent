"""Tests for hermes_cli.identity — instance name / id helpers."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from hermes_cli import identity


# ------------------------------------------------------------------
# Slug validation
# ------------------------------------------------------------------

@pytest.mark.parametrize(
    "name, valid",
    [
        ("hermes", True),
        ("rtx5090", True),
        ("a", True),
        ("a-b_c.d", True),
        ("x" * 32, True),
        ("", False),
        ("UPPER", False),
        ("has space", False),
        ("-lead", False),
        ("trail-", False),
        ("x" * 33, False),
        ("ünicode", False),
    ],
)
def test_validate_slug(name: str, valid: bool) -> None:
    assert identity.validate_instance_slug(name) is valid


# ------------------------------------------------------------------
# Config reads
# ------------------------------------------------------------------

@pytest.fixture()
def tmp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    return tmp_path


def _write_config(home: Path, content: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(textwrap.dedent(content), encoding="utf-8")


def test_identity_name_default(tmp_home: Path) -> None:
    _write_config(tmp_home, "")
    assert identity.identity_name() == "hermes"


def test_identity_name_explicit(tmp_home: Path) -> None:
    _write_config(tmp_home, """\
        identity:
          name: rtx5090
          id: "inst-abc"
        """)
    assert identity.identity_name() == "rtx5090"
    assert identity.instance_label() == "rtx5090"
    assert identity.identity_id() == "inst-abc"


def test_instance_label_none_when_default(tmp_home: Path) -> None:
    _write_config(tmp_home, """\
        identity:
          name: hermes
        """)
    assert identity.instance_label() is None


def test_instance_label_none_when_missing(tmp_home: Path) -> None:
    _write_config(tmp_home, "")
    assert identity.instance_label() is None
    assert identity.identity_id() is None

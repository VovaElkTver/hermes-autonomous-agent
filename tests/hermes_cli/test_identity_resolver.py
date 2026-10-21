"""Tests for hermes_cli.identity_resolver — transport→position resolution."""

from __future__ import annotations

import json
import textwrap
from datetime import datetime, timezone, UTC
from pathlib import Path

import pytest

from hermes_cli.identity_resolver import (
    Binding,
    IdentityResolver,
    UNREGISTERED,
    VALID_KINDS,
    append_event,
    format_envelope,
)


# ------------------------------------------------------------------
# Resolver
# ------------------------------------------------------------------

def test_resolve_known() -> None:
    r = IdentityResolver.from_dict(
        {
            "matrix:@alice:mx.elk.mobi": Binding(
                transport="matrix:@alice:mx.elk.mobi",
                position_id="agent-alice-001",
            ),
        }
    )
    assert r.resolve("matrix:@alice:mx.elk.mobi") == "agent-alice-001"


def test_resolve_unknown_returns_unregistered() -> None:
    r = IdentityResolver.from_dict(
        {
            "matrix:@alice:mx.elk.mobi": Binding(
                transport="matrix:@alice:mx.elk.mobi",
                position_id="agent-alice-001",
            ),
        }
    )
    result = r.resolve("matrix:@bob:mx.elk.mobi")
    assert result == UNREGISTERED
    # Must NOT silently fall back to "hermes"
    assert result != "hermes"


def test_resolve_empty_string() -> None:
    r = IdentityResolver()
    assert r.resolve("") == UNREGISTERED


def test_resolve_none_input() -> None:
    r = IdentityResolver()
    assert r.resolve("") == UNREGISTERED  # None coerces to ""


def test_is_registered() -> None:
    r = IdentityResolver.from_dict(
        {
            "a2a:hermes-rtx5090": Binding(
                transport="a2a:hermes-rtx5090",
                position_id="pos-001",
            ),
        }
    )
    assert r.is_registered("a2a:hermes-rtx5090") is True
    assert r.is_registered("unknown") is False
    assert "a2a:hermes-rtx5090" in r


def test_register() -> None:
    r = IdentityResolver()
    r.register("mq:hermes-win", "pos-win-1", host="10.0.0.100", path="/srv/hermes")
    assert r.resolve("mq:hermes-win") == "pos-win-1"
    assert len(r) == 1


def test_register_empty_raises() -> None:
    r = IdentityResolver()
    with pytest.raises(ValueError, match="non-empty"):
        r.register("", "pos-1")
    with pytest.raises(ValueError, match="non-empty"):
        r.register("mq:x", "")


def test_from_registry_file(tmp_path: Path) -> None:
    reg = tmp_path / "registry.json"
    reg.write_text(
        json.dumps(
            {
                "bindings": [
                    {
                        "transport": "matrix:@vovak:mx.elk.mobi",
                        "position_id": "architect-001",
                        "host": "10.0.0.2",
                        "path": "/opt/hermes",
                    },
                    {
                        "transport": "a2a:hermes-rtx",
                        "position_id": "worker-rtx",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    r = IdentityResolver.from_registry(reg)
    assert len(r) == 2
    assert r.resolve("matrix:@vovak:mx.elk.mobi") == "architect-001"
    assert r.resolve("a2a:hermes-rtx") == "worker-rtx"
    assert r.resolve("unknown") == UNREGISTERED
    b = r.binding("a2a:hermes-rtx")
    assert b is not None and b.host == ""


def test_from_registry_missing_file(tmp_path: Path) -> None:
    r = IdentityResolver.from_registry(tmp_path / "nonexistent.json")
    assert len(r) == 0
    assert r.resolve("anything") == UNREGISTERED


def test_from_registry_malformed(tmp_path: Path) -> None:
    reg = tmp_path / "bad.json"
    reg.write_text("not json{", encoding="utf-8")
    r = IdentityResolver.from_registry(reg)
    assert len(r) == 0


# ------------------------------------------------------------------
# Envelope format
# ------------------------------------------------------------------

def test_format_envelope_fields() -> None:
    ts = datetime(2025, 10, 20, 12, 0, 0, tzinfo=UTC)
    line = format_envelope(
        ts=ts,
        from_id="architect-001",
        to_id="worker-rtx",
        about="task-9d0d38a24b",
        kind="просьба",
        msg_id="msg-001",
        transport="matrix",
        correlation_id="corr-abc",
    )
    parts = line.split(" | ")
    assert len(parts) == 7
    assert parts[0] == "2025-10-20T12:00:00+00:00"
    assert parts[1] == "architect-001 -> worker-rtx"
    assert parts[2] == "about=task-9d0d38a24b"
    assert parts[3] == "kind=просьба"
    assert parts[4] == "msg-001"
    assert parts[5] == "matrix"
    assert parts[6] == "corr-abc"


def test_format_envelope_auto_msg_id() -> None:
    line = format_envelope(
        from_id="a",
        to_id="b",
        about="obj-1",
        kind="информирование",
        transport="a2a",
    )
    parts = line.split(" | ")
    assert len(parts) == 7
    # msg_id is auto-generated UUID
    assert len(parts[4]) == 36
    # correlation_id defaults to "-"
    assert parts[6] == "-"


def test_format_envelope_invalid_kind_raises() -> None:
    with pytest.raises(ValueError, match="not in VALID_KINDS"):
        format_envelope(
            from_id="a",
            to_id="b",
            about="x",
            kind="nonsense",
            transport="direct",
        )


@pytest.mark.parametrize("kind", VALID_KINDS)
def test_format_envelope_all_kinds(kind: str) -> None:
    line = format_envelope(
        from_id="a",
        to_id="b",
        about="x",
        kind=kind,
        transport="direct",
    )
    assert f"kind={kind}" in line


# ------------------------------------------------------------------
# append_event
# ------------------------------------------------------------------

def test_append_event_creates_file(tmp_path: Path) -> None:
    target = tmp_path / "sub" / "events.log"
    line = format_envelope(
        from_id="a",
        to_id="b",
        about="x",
        kind="передача",
        transport="mq",
    )
    append_event(target, line)
    content = target.read_text(encoding="utf-8")
    assert line in content
    assert content.endswith("\n")


def test_append_event_appends(tmp_path: Path) -> None:
    target = tmp_path / "events.log"
    l1 = format_envelope(from_id="a", to_id="b", about="x", kind="просьба", transport="mq")
    l2 = format_envelope(from_id="b", to_id="c", about="y", kind="согласование", transport="matrix")
    append_event(target, l1)
    append_event(target, l2)
    lines = target.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2

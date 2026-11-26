"""L3 (TZ-memdoor-5090-v2): roster mention expansion — flag-off passthrough,
bare alias expansion, full-mxid and code-span immunity."""
import pytest

from plugins.platforms.matrix import bot_roster as br

R = {"hermes": "@hermes:mx.elk.mobi", "rtx5090": "@rtx5090:mx.elk.mobi"}


def test_flag_off_passthrough(monkeypatch):
    monkeypatch.setattr(br, "bot_relay_mentions_enabled", lambda: False)
    assert br.maybe_expand("ping @hermes now") == "ping @hermes now"


def test_bare_alias_expands(monkeypatch):
    monkeypatch.setattr(br, "bot_relay_mentions_enabled", lambda: True)
    monkeypatch.setattr(br, "roster", lambda: R)
    out = br.maybe_expand("ping @hermes now")
    assert out == "ping @hermes:mx.elk.mobi now"


def test_full_mxid_not_double_expanded(monkeypatch):
    assert br.expand_outside_code("cc @hermes:mx.elk.mobi", R) == "cc @hermes:mx.elk.mobi"


def test_code_spans_untouched(monkeypatch):
    assert br.expand_outside_code("run `@hermes` and @hermes", R) == \
        "run `@hermes` and @hermes:mx.elk.mobi"
    assert br.expand_outside_code("```\n@hermes block\n``` done @hermes", R) == \
        "```\n@hermes block\n``` done @hermes:mx.elk.mobi"


def test_no_alias_no_change(monkeypatch):
    assert br.expand_outside_code("nothing here @someone", R) == "nothing here @someone"
    # substring safety: @hermesz must not match
    assert br.expand_outside_code("@hermesz", R) == "@hermesz"

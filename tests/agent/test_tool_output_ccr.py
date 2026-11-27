"""Tier L1 CCR regression tests (TZ-tinyhumansai-5090-v1).

Contracts under test: opt-in gate, compression shape, never-grows, byte-identical
retrieve, idempotency, exempt tools, multimodal passthrough, hash store semantics.
"""

import pytest

from agent import tool_output_ccr as ccr


BIG = ("x" * 1000 + "\nCRITICAL-MIDDLE-ЛИНИЯ\n" + "y" * 9000)


@pytest.fixture
def on(tmp_path, monkeypatch):
    """Enabled module pointed at a tmp store (config untouched on disk)."""
    monkeypatch.setattr(ccr, "enabled", lambda: True)
    monkeypatch.setattr(ccr, "store_dir", lambda: str(tmp_path))
    return tmp_path


def test_disabled_is_passthrough(monkeypatch):
    monkeypatch.setattr(ccr, "enabled", lambda: False)
    assert ccr.maybe_compress("web_search", BIG) is BIG


def test_compress_view_and_retrieve(on):
    out = ccr.maybe_compress("web_search", BIG)
    assert out is not BIG
    assert len(out) < len(BIG)
    assert "[ccr omitted" in out
    import re
    m = re.search(r"\[ccr:([0-9a-f]{16})", out)
    assert m, "hash marker missing"
    # byte-exact retrieve
    assert ccr.retrieve(m.group(1)) == BIG


def test_retrieve_head_tail_inline(on):
    out = ccr.maybe_compress("web_extract", BIG)
    assert out.startswith("x" * 100)
    assert "y" * 100 in out[-800:]


def test_never_grows(on, monkeypatch):
    # params tuned so head+tail+footer would EXCEED a 600-char body -> raw must stay
    monkeypatch.setattr(ccr, "_params", lambda cfg: (600, 300, 200, ccr.DEFAULT_EXEMPT_TOOLS))
    text = "z" * 600
    assert ccr.maybe_compress("web_search", text) is text


def test_idempotent_no_double_wrap(on):
    once = ccr.maybe_compress("web_search", BIG)
    assert ccr.maybe_compress("web_search", once) is once


def test_exempt_tool_untouched(on):
    assert ccr.maybe_compress("read_file", BIG) is BIG


def test_multimodal_list_untouched(on):
    parts = [{"type": "text", "text": BIG}, {"type": "image_url", "image_url": {"url": "x"}}]
    assert ccr.maybe_compress("mcp_x__y", parts) is parts


def test_store_idempotent_by_hash(on):
    h1 = ccr.store(BIG)
    h2 = ccr.store(BIG)
    assert h1 == h2
    assert len(list(on.glob("*.txt"))) == 1


def test_retrieve_malformed_hash(on):
    assert ccr.retrieve("ZZZZ") is None
    assert ccr.retrieve("") is None
    assert ccr.retrieve("deadbeefdeadbeef") is None  # well-formed, absent


def test_make_tool_result_message_integration(on):
    from agent import tool_dispatch_helpers as tdh
    msg = tdh.make_tool_result_message("web_search", BIG, "call_1")
    assert "[ccr:" in msg["content"]
    # untrusted wrap preserved: compression happened INSIDE the boundary, boundary intact
    assert "untrusted_tool_result" in msg["content"]
    import re
    h = re.search(r"\[ccr:([0-9a-f]{16})", msg["content"]).group(1)
    assert ccr.retrieve(h) == BIG


def test_ccr_retrieve_tool(on):
    from tools.ccr_tool import ccr_retrieve
    h = ccr.store(BIG)
    assert ccr_retrieve(hash=h) == BIG
    err = ccr_retrieve(hash="0123456789abcdef")
    assert isinstance(err, str) and "error" in err.lower()

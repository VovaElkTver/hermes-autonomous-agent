"""ccr_retrieve — Tier L1 retrieval tool for cached tool outputs (TZ-tinyhumansai).

The companion of agent.tool_output_ccr: when a tool result was replaced by a lossy
view with a ``[ccr:HASH]`` footer, this tool returns the byte-exact original. Kept
deliberately dumb — hash in, text out, no search — so the compression contract stays
testable in one module pair.
"""

from __future__ import annotations

from agent import tool_output_ccr
from tools.registry import registry, tool_error

CCR_RETRIEVE_SCHEMA = {
    "name": "ccr_retrieve",
    "description": (
        "Return the byte-exact full tool output cached behind a [ccr:HASH] marker. "
        "Use when a compressed tool result's visible head/tail slice is not enough — "
        "the hash sits in the result footer, e.g. ccr_retrieve(hash='1a2b3c4d5e6f7890')."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "hash": {"type": "string", "description": "The 16-hex-char hash from the [ccr:...] footer."},
        },
        "required": ["hash"],
    },
}


def ccr_retrieve(hash: str = "", **_kw):
    if not hash:
        return tool_error("ccr_retrieve: 'hash' is required (16 hex chars from a [ccr:...] footer).")
    text = tool_output_ccr.retrieve(hash)
    if text is None:
        return tool_error(
            f"ccr_retrieve: no cached output for hash {hash!r} "
            "(cache pruned, hash malformed, or output predates CCR).")
    return text


registry.register(
    name="ccr_retrieve",
    toolset="ccr_retrieve",
    schema=CCR_RETRIEVE_SCHEMA,
    handler=lambda args, **kw: ccr_retrieve(hash=args.get("hash", "")),
    emoji="🗜️")

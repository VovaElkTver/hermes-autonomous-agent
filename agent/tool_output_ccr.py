"""CCR (Compress–Cache–Retrieve) for tool outputs — Tier L1 of TZ-tinyhumansai-5090-v1.

Mechanism anchor (contract-only, no code copied — GPL source read at
~/zoo/tinyhumansai/tinyjuice crates/tinyjuice/src/compress.rs + cache/marker.rs @
ceb4f293cf17): a large tool output is replaced in-context by a lossy head+tail view
whose footer carries a hash marker; the byte-exact original is stored on disk keyed by
that hash and is retrievable at any later turn. Hard contracts:

  * never-grows — if the lossy view is not smaller than the raw content, the raw stays
    (never pay tokens for compression);
  * byte-identical retrieve — the stored blob is the exact string that was replaced;
  * idempotent — content already carrying a CCR marker is never re-compressed (a
    retrieved-then-re-injected result must not double-wrap);
  * exempt tools — results whose own contract is pagination/bounded (read_file,
    execute_code, session_search, ccr_retrieve) are left alone;
  * opt-in — ``tool_output_ccr.enabled`` defaults to False; when disabled every entry
    point is a zero-work passthrough.

This module must never raise into the tool pipeline: any storage/config error falls
back to returning the raw content.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from typing import Any, Optional

# Matches the footer marker; the same token guards idempotency (re-compression skip).
_MARKER_RE = re.compile(r"\[ccr:([0-9a-f]{16})\]")

# Tools whose output contracts already bound/paginate the payload — cutting them again
# would break their own continuation semantics (offset/next_offset, head/tail notices).
DEFAULT_EXEMPT_TOOLS = frozenset({
    "read_file", "execute_code", "session_search", "ccr_retrieve", "search_files",
})

_MIN_CHARS_FLOOR = 512          # below this, compression never pays off meaningfully
_STORE_SUBPATH = ("cache", "ccr")  # under HERMES_HOME, same family as documents cache


def _cfg() -> dict:
    try:
        try:
            from hermes_cli.config import load_config_readonly as _load
        except ImportError:
            from hermes_cli.config import load_config as _load
        value = (_load() or {}).get("tool_output_ccr")
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def enabled() -> bool:
    return bool(_cfg().get("enabled", False))


def _params(cfg: dict) -> tuple[int, int, int, frozenset]:
    min_chars = max(_MIN_CHARS_FLOOR, int(cfg.get("min_chars", 8192)))
    head = max(256, int(cfg.get("keep_head", 1200)))
    tail = max(256, int(cfg.get("keep_tail", 800)))
    exempt_raw = cfg.get("exempt_tools")
    exempt = frozenset(exempt_raw) if isinstance(exempt_raw, list) and exempt_raw else DEFAULT_EXEMPT_TOOLS
    return min_chars, head, tail, exempt


def store_dir() -> str:
    try:
        from hermes_constants import get_hermes_home
        base = str(get_hermes_home() or os.path.expanduser("~/.hermes"))
    except Exception:
        base = os.path.expanduser("~/.hermes")
    return os.path.join(base, *_STORE_SUBPATH)


def _hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8", errors="surrogatepass")).hexdigest()[:16]


def _blob_path(h: str) -> str:
    return os.path.join(store_dir(), h + ".txt")


def store(content: str) -> str:
    """Persist raw content, return its hash. Idempotent by content hash (fsync, atomic)."""
    h = _hash(content)
    path = _blob_path(h)
    if os.path.exists(path):
        return h
    directory = store_dir()
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", errors="surrogatepass") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return h


def retrieve(h: str) -> Optional[str]:
    """Byte-exact original for a marker hash; None when absent or hash shape invalid."""
    if not isinstance(h, str) or not re.fullmatch(r"[0-9a-f]{16}", h):
        return None
    try:
        with open(_blob_path(h), "r", encoding="utf-8", errors="surrogatepass") as fh:
            return fh.read()
    except OSError:
        return None


def _extract_text(content: Any) -> Optional[str]:
    """CCR acts on plain tool-result strings. Multimodal part-lists ride untouched
    (their contract is handled in thL2+ where image budget lives)."""
    return content if isinstance(content, str) else None


def maybe_compress(name: str, content: Any) -> Any:
    """Compress one tool result at construction time. Never raises; disabled/exempt/short/
    already-marked/multimodal content passes through unchanged."""
    if not enabled():
        return content
    text = _extract_text(content)
    if text is None:
        return content
    cfg = _cfg()
    min_chars, head, tail, exempt = _params(cfg)
    if name in exempt or len(text) < min_chars or _MARKER_RE.search(text):
        return content
    try:
        omitted = len(text) - head - tail
        view = (
            text[:head]
            + f"\n…[ccr omitted {omitted} chars]…\n"
            + text[-tail:]
        )
        footer_tpl = "\n[ccr:XXXXXXXXXXXXXXXX — full output cached; ccr_retrieve(\"HASH\") returns it byte-exact]"
        if len(view) + len(footer_tpl) >= len(text):
            return content  # never-grows: compression must pay for itself
        h = store(text)
        footer = footer_tpl.replace("XXXXXXXXXXXXXXXX", h).replace("HASH", h)
        return view + footer
    except Exception:
        return content

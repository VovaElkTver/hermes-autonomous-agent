"""S2 (TZ-memdoor-5090-v2): savings ledger — the receipt of what did NOT enter context.

Donor: pkg/savings/savings.go — an append-only JSONL, one line per event, never
rewritten. We record what the truncations that already exist discarded: ``raw_b``
is the original size, ``kept_b`` is what actually entered context; the difference
is the savings. Approximate tokens are ``bytes / 4`` (the donor's "tokens are
bytes over four"). Dollars are priced at the CHEAPEST model the month actually
ran on, so the figure is a floor, not a flattering one — a price hint must be
supplied by the caller; we never invent a price.

OFF by default (``savings_ledger.enabled=false`` / ``HERMES_SAVINGS_LEDGER``):
``record()`` is a no-op and the file is never written — byte-for-byte pre-S2.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any
from datetime import UTC

_ENV = "HERMES_SAVINGS_LEDGER"
_OFF = {"0", "false", "no", "off"}

# Config-derived flag, cached so the hot record() path does not reload config on
# every tool output. The env override is always read fresh (cheap) and wins.
_config_cache: bool | None = None


def _clear_cache() -> None:
    global _config_cache
    _config_cache = None


def savings_ledger_enabled() -> bool:
    """``savings_ledger.enabled`` (default **False**) / ``HERMES_SAVINGS_LEDGER``.

    OFF is the safe pre-S2 behavior: no ledger file is ever created."""
    global _config_cache
    env = os.environ.get(_ENV)
    if env is not None:
        return env.strip().lower() not in _OFF
    if _config_cache is not None:
        return _config_cache
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
    except Exception:
        _config_cache = False
        return False
    val = (cfg.get("savings_ledger") or {}).get("enabled")
    _config_cache = bool(val) if val is not None else False
    return _config_cache


def ledger_path() -> str:
    """The append-only ledger: ``~/.hermes/savings.jsonl``."""
    return os.path.join(os.path.expanduser("~"), ".hermes", "savings.jsonl")


def record(tool: str, raw_b: int, kept_b: int, *, event: str | None = None,
           ts: float | None = None, path: str | None = None) -> bool:
    """Append one event ``{ts,tool,raw_b,kept_b}``. No-op when OFF or nothing was saved.

    Never raises: a ledger write failure must not break the tool that produced the
    output. Returns True when a line was appended."""
    if not savings_ledger_enabled():
        return False
    raw_b = max(0, int(raw_b))
    kept_b = max(0, int(kept_b))
    if raw_b <= 0 and kept_b <= 0:
        return False
    rec: dict[str, Any] = {
        "ts": ts if ts is not None else time.time(),
        "tool": tool,
        "raw_b": raw_b,
        "kept_b": kept_b,
    }
    if event:
        rec["event"] = event
    p = path or ledger_path()
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False


def load_events(path: str | None = None, month: str | None = None) -> list[dict]:
    """Read the ledger (append-only), optionally filtered to a UTC ``YYYY-MM`` month."""
    p = path or ledger_path()
    if not os.path.exists(p):
        return []
    events: list[dict] = []
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            if not isinstance(e, dict):
                continue
            if month:
                from datetime import datetime, timezone

                t = e.get("ts")
                try:
                    ym = datetime.fromtimestamp(float(t), tz=UTC).strftime("%Y-%m")
                except (TypeError, ValueError, OverflowError, OSError):
                    continue
                if ym != month:
                    continue
            events.append(e)
    return events


def _saved(e: dict) -> int:
    return max(0, int(e.get("raw_b") or 0) - int(e.get("kept_b") or 0))


def summarize(month: str | None = None, path: str | None = None,
              price_usd_per_mtok: float | None = None) -> dict:
    """MB not in context, ≈tokens (bytes/4), ≈$ floor.

    ``approx_usd_floor`` is computed only when ``price_usd_per_mtok`` (the cheapest
    model actually run in the month, $/M tokens) is supplied — a floor, not a
    flattering number; otherwise it is None (price unknown, not fabricated)."""
    events = load_events(path, month)
    raw = sum(int(e.get("raw_b") or 0) for e in events)
    kept = sum(int(e.get("kept_b") or 0) for e in events)
    saved = max(0, raw - kept)
    toks = saved // 4
    usd = None
    if price_usd_per_mtok is not None:
        usd = (toks / 1_000_000) * float(price_usd_per_mtok)
    by_tool: dict[str, int] = {}
    for e in events:
        t = str(e.get("tool") or "?")
        by_tool[t] = by_tool.get(t, 0) + _saved(e)
    return {
        "events": len(events),
        "raw_b": raw,
        "kept_b": kept,
        "saved_b": saved,
        "saved_mb": round(saved / 1e6, 6),
        "approx_tokens": toks,
        "approx_usd_floor": usd,
        "by_tool_saved_b": by_tool,
        "month": month,
    }

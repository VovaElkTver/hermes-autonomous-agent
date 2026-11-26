"""Per-agent iteration budget — thread-safe consume/refund counter.

Each ``AIAgent`` (parent or subagent) holds its own :class:`IterationBudget`: the parent's
cap is ``max_iterations`` (default 500), each subagent's ``delegation.max_iterations``
(default 50), so total iterations across parent + subagents can exceed the parent's cap.
"""

from __future__ import annotations

import math
import threading


def normalize_budget_warning_ratio(value) -> float | None:
    """A finite ratio strictly between zero and one, or None (feature off)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        ratio = float(value)
    except (TypeError, ValueError):
        return None
    return ratio if math.isfinite(ratio) and 0 < ratio < 1 else None


def normalize_budget_notice_thresholds(value) -> list:
    """Sorted, de-duplicated thresholds strictly between 0 and 1, else ``[]``.

    A non-empty result switches the iteration-budget checkpoint from the
    one-shot warning to a stepped notice sequence (first threshold = progress
    check, each later one = wrap-up). An empty list (the default / OFF) keeps
    the one-shot behaviour byte-for-byte. Non-numeric, boolean, non-finite and
    out-of-range entries are dropped; a bare scalar is treated as a
    one-element list; a bare string is not a threshold list.
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple, set, frozenset)):
        seq = list(value)
    elif isinstance(value, str):
        return []
    else:
        seq = [value]
    seen = set()
    out = []
    for item in seq:
        if isinstance(item, bool):
            continue
        try:
            th = float(item)
        except (TypeError, ValueError):
            continue
        if math.isfinite(th) and 0 < th < 1 and th not in seen:
            seen.add(th)
            out.append(th)
    return sorted(out)


class IterationBudget:
    """Thread-safe iteration counter; ``execute_code`` (programmatic tool calling)
    iterations are refunded via :meth:`refund` so they don't eat into the budget."""

    def __init__(self, max_total: int):
        self.max_total = max_total
        self._used = 0
        self._lock = threading.Lock()

    def consume(self) -> bool:
        """Try to consume one iteration.  Returns True if allowed."""
        with self._lock:
            if self._used >= self.max_total:
                return False
            self._used += 1
            return True

    def refund(self) -> None:
        """Give back one iteration (e.g. for execute_code turns)."""
        with self._lock:
            if self._used > 0:
                self._used -= 1

    @property
    def used(self) -> int:
        with self._lock:
            return self._used

    @property
    def remaining(self) -> int:
        with self._lock:
            return max(0, self.max_total - self._used)


__all__ = ["IterationBudget", "normalize_budget_notice_thresholds",
           "normalize_budget_warning_ratio"]

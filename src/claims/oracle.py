"""Mechanism 3 — the oracle lives outside the candidate and the budget is
enforced on the server side.

* ``eval_fn`` is FROZEN and not visible to the executor: it is stored under a
  private name and the only public entry point is :meth:`FrozenOracle.evaluate`.
* Every evaluation increments a server-side counter (the "single budget choke
  point", cf. terrarium's EvalServer). Once the budget is spent, further
  evaluations are refused.
* A frozen-hash fingerprint of the eval function is recorded on every result so
  a later audit can prove the scorer was not swapped out mid-run.

No DSPy / terrarium runtime is imported; this is a plain stdlib stand-in that
mirrors the terrarium choke-point shape.
"""

from __future__ import annotations

import hashlib
import inspect
from typing import Any, Callable, Dict, Tuple


class BudgetExhausted(RuntimeError):
    """Raised when a frozen oracle is asked to evaluate past its budget."""


def default_eval_fn(claim_result) -> float:
    """Default hidden scorer: fraction of the FULL population that passed.

    Kept deliberately tiny and deterministic. The candidate never sees this
    source; it only ever receives the resulting scalar.
    """
    total = claim_result.total
    if not total:
        return 1.0
    return claim_result.passed / total


def _fingerprint(eval_fn: Callable) -> str:
    """Best-effort stable hash of the eval function source (immutability marker)."""
    try:
        src = inspect.getsource(eval_fn)
    except (OSError, TypeError):
        src = repr(eval_fn)
    return hashlib.sha256(src.encode("utf-8", "replace")).hexdigest()


class FrozenOracle:
    """Server-side frozen evaluator with a hard budget.

    The candidate interacts with the oracle exclusively through
    :meth:`evaluate`; it cannot read or replace the underlying scorer.
    """

    def __init__(self, eval_fn: Callable, budget: int) -> None:
        if budget < 1:
            raise ValueError("oracle budget must be >= 1")
        # Deliberately NOT exposed as ``self.eval_fn`` so a candidate cannot
        # introspect or monkeypatch the scorer.
        object.__setattr__(self, "_ev", eval_fn)
        self._budget: int = budget
        self._used: int = 0
        self._frozen: str = _fingerprint(eval_fn)

    def evaluate(self, claim_result) -> Tuple[float, Dict[str, Any]]:
        if self._used >= self._budget:
            raise BudgetExhausted(
                f"oracle budget exhausted ({self._used}/{self._budget})"
            )
        self._used += 1
        score = self._ev(claim_result)
        meta = {
            "budget_used": self._used,
            "budget_total": self._budget,
            "frozen_hash": self._frozen,
        }
        return float(score), meta

    # Read-only observability for the harness / audit trail.
    @property
    def frozen_hash(self) -> str:
        return self._frozen

    @property
    def budget_used(self) -> int:
        return self._used

    @property
    def budget_total(self) -> int:
        return self._budget

    @property
    def budget_remaining(self) -> int:
        return self._budget - self._used

"""claims: an external oracle for agent analytical work.

The candidate (an agent) may NOT define its own success. The harness:
  1. enumerates the full work population itself (Mechanism 1),
  2. re-runs the agent's machine-checkable predicate over the FULL population
     and returns a distribution of counterexamples (Mechanism 2),
  3. scores through a frozen, hidden, budgeted oracle that the candidate
     cannot see or bypass (Mechanism 3).

Stage 1 deliverable. No DSPy / terrarium runtime is imported; stdlib only.
"""

from .core import (
    Item,
    Population,
    ItemResult,
    Claim,
    ClaimResult,
    Coverage,
    Trace,
    ClaimHarness,
    render_report,
)
from .oracle import FrozenOracle, BudgetExhausted, default_eval_fn
from . import xlsx_io

__all__ = [
    "Item",
    "Population",
    "ItemResult",
    "Claim",
    "ClaimResult",
    "Coverage",
    "Trace",
    "ClaimHarness",
    "render_report",
    "FrozenOracle",
    "BudgetExhausted",
    "default_eval_fn",
    "xlsx_io",
]

__version__ = "0.1.0"

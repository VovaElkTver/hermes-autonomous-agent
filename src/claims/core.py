"""Core data model for the claims harness.

The central idea is separation of powers:

* The *population* (the set of work items) is owned and enumerated by the
  harness, never by the candidate. This makes silent population substitution
  detectable: the harness always scores against the full set it enumerated.

* The *candidate* (agent) submits a *Claim*: a natural-language assertion plus
  a machine-checkable *predicate* it claims describes the data. The harness
  runs that predicate against every item in the full population and collects
  every counterexample as its own line.

* The *trace* records which items the candidate actually read. Coverage is
  computed from the trace against the full population. If the candidate
  asserted "all N items satisfy X" but only read M < N of them, the harness
  flags that as incomplete coverage / potential silent substitution.

* Scoring goes through a frozen oracle (see oracle.py); it is not part of the
  candidate's context.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set


# --------------------------------------------------------------------------- #
# Items & population
# --------------------------------------------------------------------------- #
@dataclass
class Item:
    """One atomic unit of work (a file, a DB row, an xlsx sheet, a PDF page)."""

    id: str
    label: str
    payload: Any = None


class Population:
    """Deterministic, harness-owned enumeration of the work items.

    The candidate cannot define or shrink this set; the harness builds it from
    the source-of-truth (a directory, a table, a workbook) and keeps it
    immutable. ``enumerate_population`` in a work-class module is the only
    authorized way to build it.
    """

    def __init__(self, name: str, source: str, items: List[Item]):
        self.name = name
        self.source = source
        self._items: List[Item] = list(items)
        self._by_id: Dict[str, Item] = {i.id: i for i in self._items}

    def items(self) -> List[Item]:
        return list(self._items)

    def by_id(self, item_id: str) -> Item:
        return self._by_id[item_id]

    def __len__(self) -> int:
        return len(self._items)

    @property
    def ids(self) -> List[str]:
        return [i.id for i in self._items]


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #
@dataclass
class ItemResult:
    """Outcome of running the candidate's predicate against one item."""

    item_id: str
    ok: bool
    detail: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Coverage:
    covered: List[str]
    missed: List[str]
    extra: List[str]  # items the trace touched that are not in the population
    ratio: float
    total: int

    @property
    def complete(self) -> bool:
        return not self.missed and not self.extra


class Trace:
    """Records which items the candidate actually read/touched in its session.

    The harness uses this to compute coverage. The candidate does NOT get to
    declare the population; it only gets to leave a trace of what it read.
    """

    def __init__(self) -> None:
        self.read: Set[str] = set()

    def record_read(self, item_id: str) -> None:
        self.read.add(item_id)

    def coverage(self, population: Population) -> Coverage:
        pop = set(population.ids)
        covered = sorted(pop & self.read)
        missed = sorted(pop - self.read)
        extra = sorted(self.read - pop)
        total = len(pop)
        ratio = (len(covered) / total) if total else 1.0
        return Coverage(
            covered=covered,
            missed=missed,
            extra=extra,
            ratio=ratio,
            total=total,
        )


class Claim:
    """The candidate's assertion: a statement plus a machine-checkable predicate.

    ``predicate`` maps an :class:`Item` to an :class:`ItemResult`. It is the
    candidate's machine-checkable encoding of its claim. The harness runs it
    over the *full* population, independent of the candidate's trace.

    ``population_hint`` (optional) lets us detect when a candidate tries to
    smuggle in its own (smaller/different) population — the harness ignores
    it for scoring but records the mismatch as a substitution signal.
    """

    def __init__(
        self,
        statement: str,
        predicate: Callable[[Item], ItemResult],
        population_hint: Optional[Population] = None,
    ) -> None:
        self.statement = statement
        self.predicate = predicate
        self._population_hint = population_hint

    def check(self, item: Item) -> ItemResult:
        return self.predicate(item)

    @property
    def population_hint(self) -> Optional[Population]:
        return self._population_hint


@dataclass
class ClaimResult:
    claim: Claim
    population: Population
    total: int
    passed: int
    counterexamples: List[ItemResult]
    coverage: Coverage
    score: float = 0.0
    score_meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def all_passed(self) -> bool:
        return not self.counterexamples

    def counterexample_in_unread(self) -> List[ItemResult]:
        """Counterexamples on items the candidate never read (silent substitution)."""
        covered = set(self.coverage.covered)
        return [ce for ce in self.counterexamples if ce.item_id not in covered]


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #
class ClaimHarness:
    """Ties population + claim + trace together behind a frozen oracle.

    The candidate only ever calls :meth:`evaluate`. It does not touch the
    oracle internals or the source-of-truth directly; scoring, budget, and
    full-population re-checking all happen here on the harness side.
    """

    def __init__(self, population: Population, oracle: "FrozenOracle") -> None:
        self.population = population
        self.oracle = oracle

    def evaluate(self, claim: Claim, trace: Trace) -> ClaimResult:
        # Re-run the candidate's predicate over the FULL harness-owned population.
        results = [claim.check(item) for item in self.population.items()]
        counterexamples = [r for r in results if not r.ok]
        passed = sum(1 for r in results if r.ok)
        coverage = trace.coverage(self.population)

        provisional = ClaimResult(
            claim=claim,
            population=self.population,
            total=len(self.population),
            passed=passed,
            counterexamples=counterexamples,
            coverage=coverage,
            score=0.0,
        )
        score, meta = self.oracle.evaluate(provisional)
        provisional.score = score
        provisional.score_meta = meta

        substitution = self._detect_substitution(claim, coverage)
        if substitution:
            provisional.score_meta["substitution"] = substitution
        return provisional

    def _detect_substitution(self, claim: Claim, coverage: Coverage):
        # Hard substitution: the candidate supplied a different population.
        hint = claim.population_hint
        if hint is not None:
            hint_ids = set(hint.ids)
            pop_ids = set(self.population.ids)
            if hint_ids != pop_ids:
                return {
                    "type": "population_substitution",
                    "hint_size": len(hint_ids),
                    "true_size": len(pop_ids),
                    "dropped": sorted(pop_ids - hint_ids),
                    "added": sorted(hint_ids - pop_ids),
                }
        # Soft substitution: candidate asserted over the whole population but
        # only read part of it, or touched items outside the population.
        if not coverage.complete:
            return {
                "type": "incomplete_coverage",
                "ratio": round(coverage.ratio, 4),
                "missed": coverage.missed,
                "extra": coverage.extra,
                "note": "candidate asserted over full population but trace is incomplete",
            }
        return None


# --------------------------------------------------------------------------- #
# Reporting (each counterexample is its own line — never "в основном")
# --------------------------------------------------------------------------- #
def _fmt_detail(detail: Dict[str, Any]) -> str:
    if not detail:
        return ""
    return " ".join(f"{k}={_fmt_val(v)}" for k, v in detail.items())


def _fmt_val(v: Any) -> str:
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def render_report(res: ClaimResult) -> str:
    cov = res.coverage
    status = "PASS" if (res.all_passed and cov.complete) else "FAIL"
    read_ids = set(cov.covered)
    lines: List[str] = []
    lines.append(f"CLAIM: {res.claim.statement}")
    lines.append(f"POPULATION: {res.population.name} ({res.total} items, source={res.population.source})")
    lines.append(
        f"COVERAGE: {len(cov.covered)}/{cov.total} read ({cov.ratio:.0%})"
    )
    if cov.missed:
        preview = ", ".join(cov.missed[:5])
        more = f" ... (+{len(cov.missed) - 5} more)" if len(cov.missed) > 5 else ""
        lines.append(f"  UNREAD: {len(cov.missed)} -> {preview}{more}")
    if cov.extra:
        lines.append(f"  OUT-OF-POPULATION: {len(cov.extra)} -> {', '.join(cov.extra[:5])}")
    lines.append(f"RESULT: {status} (predicate passed {res.passed}/{res.total})")
    lines.append(
        f"SCORE: {res.score:.4f}  [oracle; budget used {res.score_meta.get('budget_used', '?')}/"
        f"{res.score_meta.get('budget_total', '?')}; frozen={res.score_meta.get('frozen_hash', '')[:12]}]"
    )
    sub = res.score_meta.get("substitution")
    if sub:
        lines.append(f"SUBSTITUTION: {sub['type']} {_fmt_detail(sub)}")

    unread_ces = res.counterexample_in_unread()
    lines.append(
        f"COUNTEREXAMPLES ({len(res.counterexamples)}):"
        f"  [{len(unread_ces)} on items the candidate never read]"
    )
    if not res.counterexamples:
        lines.append("  (none)")
    for ce in res.counterexamples:
        read_flag = "read " if ce.item_id in read_ids else "UNREAD"
        extra = f"  <- { _fmt_detail(ce.detail) }" if ce.detail else ""
        lines.append(f"  - {ce.item_id} [{read_flag}]{extra}")
    return "\n".join(lines)

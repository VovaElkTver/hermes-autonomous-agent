#!/usr/bin/env python3
"""Demo — work class 1: xlsx column of sums (the architect's "15 out of 100").

Scenario
--------
100 workbooks are generated on disk. Column ``A`` holds 5 numbers each; the
canonical total cell is ``C1``.

* 85 "clean" workbooks: ``C1`` == sum(column A).
* 15 "dirty" workbooks (embedded counterexamples): the true total was placed in
  a *different* cell ``C2`` and ``C1`` holds a wrong value.

The simulated candidate (agent) only *reads* 70 of the 100 files and then
asserts "every workbook's C1 equals the sum of column A". The harness ignores
what the agent read, re-runs the predicate over the FULL 100-file population,
and reports every counterexample on its own line — including the 9 that the
agent never opened (the silent-substitution catch).

Run:  python3 examples/xlsx_sums/demo.py
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src")))

from claims import ClaimHarness, FrozenOracle, Trace, default_eval_fn, render_report  # noqa: E402
from claims import xlsx_io  # noqa: E402
from claims.workclasses import xlsx_sums  # noqa: E402

DIRTY_INDICES = [3, 12, 20, 35, 50, 65, 71, 74, 78, 82, 85, 88, 91, 95, 99]
N_FILES = 100
AGENT_READS = list(range(0, 70))  # agent only opens files 0..69


def _make_workbook(path: str, i: int, dirty: bool) -> None:
    values = [i * 7 + k for k in range(1, 6)]  # deterministic column A
    s = float(sum(values))
    cells = {("B", 1): "TOTAL"}
    for k, v in enumerate(values):
        cells[("A", 2 + k)] = float(v)
    if dirty:
        cells[("C", 1)] = s + 100.0  # wrong value in the canonical cell
        cells[("C", 2)] = s  # the real sum lives in a DIFFERENT cell
    else:
        cells[("C", 1)] = s
    xlsx_io.write_xlsx(path, cells)


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="claims-xlsx-")
    for i in range(N_FILES):
        _make_workbook(os.path.join(tmp, f"ledger_{i:03d}.xlsx"), i, i in DIRTY_INDICES)

    # --- harness side (the agent does not define the population) -----------
    population = xlsx_sums.enumerate_population(tmp)
    oracle = FrozenOracle(default_eval_fn, budget=5)
    harness = ClaimHarness(population, oracle)

    # --- candidate side (simulated agent) ---------------------------------
    claim = xlsx_sums.build_sum_claim()
    trace = Trace()
    for i in AGENT_READS:
        trace.record_read(f"ledger_{i:03d}.xlsx")

    result = harness.evaluate(claim, trace)

    print("=== claims-harness :: xlsx column-of-sums demo ===")
    print(f"generated {N_FILES} workbooks; embedded counterexamples: {sorted(DIRTY_INDICES)}")
    print(f"agent read {len(AGENT_READS)}/{N_FILES} files\n")
    print(render_report(result))
    print()

    detected = sorted(ce.item_id for ce in result.counterexamples)
    expected = sorted(f"ledger_{i:03d}.xlsx" for i in DIRTY_INDICES)
    unread_ce = sorted(ce.item_id for ce in result.counterexample_in_unread())
    print("--- verification ---")
    print(f"embedded : {len(expected)}")
    print(f"detected : {len(detected)}")
    print(f"match    : {detected == expected}")
    print(f"counterexamples the agent NEVER read: {len(unread_ce)} (silent-substitution catch)")
    ok = detected == expected and len(result.counterexamples) == len(DIRTY_INDICES)
    print(f"DEMO {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

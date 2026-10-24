#!/usr/bin/env python3
"""Demo — work class 2: donor analysis with coverage by trace.

Scenario
--------
A donor repo dir holds 15 source files. 13 of them define a top-level
``main()``; 2 (``mod03.py`` and ``mod08.py``) do not.

The simulated donor-analysis agent only *reads* 10 of the 15 files and then
asserts "every source file defines a top-level main()". The harness re-runs the
predicate over all 15 files (straight from disk, not from the agent's trace)
and measures coverage from the trace. Both counterexamples sit in files the
agent never opened, so they are reported as the silent-substitution class.

Run:  python3 examples/donor_coverage/demo.py
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src")))

from claims import ClaimHarness, FrozenOracle, Trace, default_eval_fn, render_report  # noqa: E402
from claims.workclasses import donor  # noqa: E402

N_FILES = 15
DIRTY = {"mod03.py", "mod08.py"}  # these two lack a top-level main()
AGENT_READS = [f"mod{i:02d}.py" for i in (1, 2, 4, 5, 6, 7, 9, 10, 11, 12)]  # 10 files, skips 3 & 8


def _make_source(path: str, has_main: bool) -> None:
    body = 'import os\n\n\n'
    if has_main:
        body += 'def main():\n    print("entry point")\n\n\nif __name__ == "__main__":\n    main()\n'
    else:
        body += 'def run():\n    print("alternate entry point")\n\n\nif __name__ == "__main__":\n    run()\n'
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="claims-donor-")
    for i in range(1, N_FILES + 1):
        name = f"mod{i:02d}.py"
        _make_source(os.path.join(tmp, name), has_main=(name not in DIRTY))

    population = donor.enumerate_population(tmp, suffix=".py")
    oracle = FrozenOracle(default_eval_fn, budget=5)
    harness = ClaimHarness(population, oracle)

    claim = donor.build_entry_point_claim()
    trace = Trace()
    for name in AGENT_READS:
        trace.record_read(name)

    result = harness.evaluate(claim, trace)

    print("=== claims-harness :: donor coverage demo ===")
    print(f"generated {N_FILES} source files; files WITHOUT main(): {sorted(DIRTY)}")
    print(f"agent read {len(AGENT_READS)}/{N_FILES} files\n")
    print(render_report(result))
    print()

    detected = sorted(ce.item_id for ce in result.counterexamples)
    expected = sorted(DIRTY)
    unread_ce = sorted(ce.item_id for ce in result.counterexample_in_unread())
    print("--- verification ---")
    print(f"embedded : {expected}")
    print(f"detected : {detected}")
    print(f"match    : {detected == expected}")
    print(f"counterexamples on files the agent NEVER read: {unread_ce}")
    ok = detected == expected and result.coverage.ratio < 1.0
    print(f"DEMO {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

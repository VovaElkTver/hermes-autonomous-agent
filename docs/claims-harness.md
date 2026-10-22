# Claims Harness

External oracle for verifying agent analytical claims.

## Problem
Agents that verify their own analytical work are unreliable — they self-optimize
and count "no error" as success. This harness moves evaluation outside the
candidate: the harness enumerates the population, the oracle is frozen and
hidden from the executor, and budget is enforced server-side.

## Quick Start

```bash
# Run tests
python3 -m unittest discover -s tests -v

# Run demo: xlsx column-sums (100 files, 15 counterexamples)
python3 examples/xlsx_sums/demo.py

# Run demo: donor coverage by trace (15 files, 2 counterexamples)
python3 examples/donor_coverage/demo.py
```

## Architecture

| Component | File | Role |
|-----------|------|------|
| Population | `src/claims/core.py` | Harness-enumerated set of work items |
| Trace | `src/claims/core.py` | Records which items the agent actually read |
| Claim | `src/claims/core.py` | Agent's assertion + machine-checkable predicate |
| Oracle | `src/claims/oracle.py` | Frozen, hidden scorer with budget (single choke point) |
| Report | `src/claims/core.py` | One line per counterexample, no hedging |

## Work Classes

- **xlsx-sums** (`src/claims/workclasses/xlsx_sums.py`): validates column totals
  across a set of workbooks. Counterexample = file where the stated sum ≠
  actual column sum.
- **donor-coverage** (`src/claims/workclasses/donor.py`): verifies that every
  source file in a donor repo satisfies a structural predicate. Counterexample
  = file that fails the predicate but was never read by the agent.

## Constraints
- No DSPy or terrarium runtime dependencies (stdlib only).
- Each counterexample gets its own report line.
- Oracle eval_fn is frozen (SHA-256 fingerprint) and not accessible to the candidate.

"""Tests for the claims harness (stdlib unittest — no external deps).

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from claims import (  # noqa: E402
    BudgetExhausted,
    Claim,
    ClaimHarness,
    FrozenOracle,
    Item,
    ItemResult,
    Population,
    Trace,
    default_eval_fn,
    render_report,
)
from claims import xlsx_io  # noqa: E402
from claims.workclasses import donor, xlsx_sums  # noqa: E402


def _mini_population(n: int = 5) -> Population:
    items = [Item(id=f"f{i}", label=f"f{i}", payload={"n": i}) for i in range(n)]
    return Population(name="mini", source="mem", items=items)


def _all_pass_claim() -> Claim:
    return Claim("all pass", lambda it: ItemResult(it.id, ok=True))


class PopulationTest(unittest.TestCase):
    def test_harness_enumerates_full_population(self):
        pop = _mini_population(5)
        self.assertEqual(len(pop), 5)
        self.assertEqual(pop.ids, ["f0", "f1", "f2", "f3", "f4"])

    def test_by_id(self):
        pop = _mini_population(3)
        self.assertEqual(pop.by_id("f2").label, "f2")


class CoverageTest(unittest.TestCase):
    def test_coverage_from_trace(self):
        pop = _mini_population(5)
        trace = Trace()
        for i in range(3):
            trace.record_read(f"f{i}")
        cov = trace.coverage(pop)
        self.assertAlmostEqual(cov.ratio, 0.6)
        self.assertEqual(cov.missed, ["f3", "f4"])
        self.assertFalse(cov.complete)

    def test_out_of_population_read_is_flagged(self):
        pop = _mini_population(2)
        trace = Trace()
        trace.record_read("f0")
        trace.record_read("zzz-not-in-pop")
        cov = trace.coverage(pop)
        self.assertEqual(cov.extra, ["zzz-not-in-pop"])
        self.assertFalse(cov.complete)


class XlsxCounterexampleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="claims-test-xlsx-")
        dirty = {3, 7}
        for i in range(10):
            path = os.path.join(self.tmp, f"l_{i:02d}.xlsx")
            vals = [float(i + k) for k in range(1, 4)]
            s = sum(vals)
            cells = {("A", 2): vals[0], ("A", 3): vals[1], ("A", 4): vals[2]}
            if i in dirty:
                cells[("C", 1)] = s + 50
                cells[("C", 2)] = s
            else:
                cells[("C", 1)] = s
            xlsx_io.write_xlsx(path, cells)

    def test_predicate_finds_all_counterexamples(self):
        pop = xlsx_sums.enumerate_population(self.tmp)
        self.assertEqual(len(pop), 10)
        claim = xlsx_sums.build_sum_claim()
        oracle = FrozenOracle(default_eval_fn, budget=100)
        harness = ClaimHarness(pop, oracle)
        res = harness.evaluate(claim, Trace())
        detected = sorted(ce.item_id for ce in res.counterexamples)
        self.assertEqual(detected, ["l_03.xlsx", "l_07.xlsx"])
        # each counterexample carries the "sum in another cell" detail
        for ce in res.counterexamples:
            self.assertEqual(ce.detail["sum_found_at"], ["C2"])
            # c1_value is the wrong value (sum+50); expected is the true sum
            self.assertEqual(ce.detail["expected_sum"], ce.detail["c1_value"] - 50)

    def test_report_has_one_line_per_counterexample(self):
        pop = xlsx_sums.enumerate_population(self.tmp)
        claim = xlsx_sums.build_sum_claim()
        oracle = FrozenOracle(default_eval_fn, budget=100)
        res = ClaimHarness(pop, oracle).evaluate(claim, Trace())
        report = render_report(res)
        lines = [ln for ln in report.splitlines() if ln.startswith("  - l_")]
        self.assertEqual(len(lines), 2)
        # no "в основном" / hedging language
        self.assertNotIn("в основном", report.lower())

    def test_clean_run_passes(self):
        # build a clean-only population
        clean = tempfile.mkdtemp(prefix="claims-test-clean-")
        for i in range(3):
            path = os.path.join(clean, f"c_{i}.xlsx")
            vals = [float(i + 1), float(i + 2)]
            cells = {("A", 2): vals[0], ("A", 3): vals[1], ("C", 1): sum(vals)}
            xlsx_io.write_xlsx(path, cells)
        pop = xlsx_sums.enumerate_population(clean)
        claim = xlsx_sums.build_sum_claim()
        oracle = FrozenOracle(default_eval_fn, budget=100)
        trace = Trace()
        for it in pop.items():
            trace.record_read(it.id)
        res = ClaimHarness(pop, oracle).evaluate(claim, trace)
        self.assertTrue(res.all_passed)
        self.assertTrue(res.coverage.complete)


class DonorCoverageTest(unittest.TestCase):
    def test_unread_counterexamples_are_caught(self):
        tmp = tempfile.mkdtemp(prefix="claims-test-donor-")
        names = [f"m{i:02d}.py" for i in range(1, 8)]  # 7 files
        dirty = {"m03.py"}
        for name in names:
            main = "def main():\n    pass\n" if name not in dirty else "def run():\n    pass\n"
            with open(os.path.join(tmp, name), "w", encoding="utf-8") as fh:
                fh.write(main)
        pop = donor.enumerate_population(tmp, suffix=".py")
        claim = donor.build_entry_point_claim()
        # agent reads only 5 of 7, and does NOT read the dirty one
        trace = Trace()
        for name in ["m01.py", "m02.py", "m04.py", "m05.py", "m06.py"]:
            trace.record_read(name)
        res = ClaimHarness(pop, FrozenOracle(default_eval_fn, 100)).evaluate(claim, trace)
        self.assertEqual([ce.item_id for ce in res.counterexamples], ["m03.py"])
        self.assertIn("m03.py", [ce.item_id for ce in res.counterexample_in_unread()])
        self.assertLess(res.coverage.ratio, 1.0)


class SubstitutionTest(unittest.TestCase):
    def test_hard_population_substitution_detected(self):
        pop = _mini_population(5)
        # candidate smuggles in a smaller population hint
        hint = _mini_population(3)
        claim = Claim("all pass", lambda it: ItemResult(it.id, ok=True), population_hint=hint)
        res = ClaimHarness(pop, FrozenOracle(default_eval_fn, 100)).evaluate(claim, Trace())
        sub = res.score_meta.get("substitution")
        self.assertIsNotNone(sub)
        self.assertEqual(sub["type"], "population_substitution")
        self.assertEqual(sub["hint_size"], 3)
        self.assertEqual(sub["true_size"], 5)

    def test_incomplete_coverage_flagged(self):
        pop = _mini_population(5)
        claim = _all_pass_claim()
        trace = Trace()
        trace.record_read("f0")
        res = ClaimHarness(pop, FrozenOracle(default_eval_fn, 100)).evaluate(claim, trace)
        sub = res.score_meta.get("substitution")
        self.assertIsNotNone(sub)
        self.assertEqual(sub["type"], "incomplete_coverage")


class OracleTest(unittest.TestCase):
    def test_budget_exhausted_raises(self):
        oracle = FrozenOracle(default_eval_fn, budget=2)
        pop = _mini_population(1)
        harness = ClaimHarness(pop, oracle)
        claim = _all_pass_claim()
        harness.evaluate(claim, Trace())
        harness.evaluate(claim, Trace())
        with self.assertRaises(BudgetExhausted):
            harness.evaluate(claim, Trace())

    def test_eval_fn_is_not_exposed(self):
        oracle = FrozenOracle(default_eval_fn, budget=5)
        # the candidate-facing surface must not reveal the scorer
        self.assertNotIn("eval_fn", dir(oracle))
        self.assertFalse(hasattr(oracle, "eval_fn"))
        self.assertEqual(oracle.budget_used, 0)
        self.assertTrue(oracle.frozen_hash)

    def test_frozen_hash_stable_across_instances(self):
        self.assertEqual(
            FrozenOracle(default_eval_fn, 1).frozen_hash,
            FrozenOracle(default_eval_fn, 1).frozen_hash,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)

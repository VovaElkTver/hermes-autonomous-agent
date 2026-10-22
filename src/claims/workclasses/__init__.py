"""Work-class package: concrete population enumerators + predicates.

Each module here is a *harness-side* population enumerator (the source of
truth the agent does not get to redefine) plus the helpers a candidate needs
to write a machine-checkable predicate.
"""

from .xlsx_sums import (
    enumerate_population as xlsx_enumerate,
    build_sum_claim,
    column_a_values,
    canonical_total,
    true_sum,
    locate_sum,
)
from .donor import (
    enumerate_population as donor_enumerate,
    build_entry_point_claim,
    defines_main,
)

__all__ = [
    "xlsx_enumerate",
    "build_sum_claim",
    "column_a_values",
    "canonical_total",
    "true_sum",
    "locate_sum",
    "donor_enumerate",
    "build_entry_point_claim",
    "defines_main",
]

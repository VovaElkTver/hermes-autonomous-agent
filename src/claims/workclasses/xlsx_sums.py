"""Work class 1 — xlsx column of sums.

The architect's reference case: "15 files with the sum in a different cell out
of 100". Each workbook has a data column (``A``) and a *canonical* total cell
(``C1``). A workbook is clean when ``C1`` equals the sum of column ``A``. In a
dirty workbook the true total lives in a *different* cell (e.g. ``C2``) and
``C1`` holds a wrong / placeholder value, so the "sum is in another cell".

Population = every ``*.xlsx`` in a directory (enumerated by the harness,
sorted for determinism).
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional

from .. import xlsx_io
from ..core import Claim, Item, ItemResult, Population

DATA_COL = "A"
CANONICAL_TOTAL_CELL = ("C", 1)
_TOTAL_LABEL = "TOTAL"


def column_a_values(item: Item) -> List[float]:
    cells: Dict = item.payload
    vals = []
    for (col, _row), value in cells.items():
        if col == DATA_COL and isinstance(value, (int, float)):
            vals.append(float(value))
    return vals


def true_sum(item: Item) -> float:
    return float(sum(column_a_values(item)))


def canonical_total(item: Item) -> Optional[float]:
    """Value of the canonical total cell C1, if it holds a number."""
    val = item.payload.get(CANONICAL_TOTAL_CELL)
    if isinstance(val, (int, float)):
        return float(val)
    return None


def locate_sum(item: Item) -> List[str]:
    """All non-data cells whose value equals the true column-A sum.

    In a clean workbook this is just ``C1``. In a dirty workbook it points at
    the *other* cell where the sum actually lives (e.g. ``C2``).
    """
    target = true_sum(item)
    hits = []
    for (col, row), value in item.payload.items():
        if col == DATA_COL:
            continue
        if isinstance(value, (int, float)) and abs(float(value) - target) < 1e-9:
            hits.append(f"{col}{row}")
    return sorted(hits)


def enumerate_population(directory: str) -> Population:
    """Harness-side enumeration of the full xlsx population."""
    items: List[Item] = []
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".xlsx"):
            continue
        path = os.path.join(directory, name)
        cells = xlsx_io.read_xlsx(path)
        items.append(Item(id=name, label=name, payload=cells))
    return Population(name="xlsx-column-sums", source=directory, items=items)


def build_sum_claim() -> Claim:
    """The candidate's machine-checkable assertion.

    Statement: "In every workbook the total in the canonical cell C1 equals
    the sum of data column A."
    """

    def predicate(item: Item) -> ItemResult:
        expected = true_sum(item)
        actual = canonical_total(item)
        if actual is not None and abs(actual - expected) < 1e-9:
            return ItemResult(item.id, ok=True, detail={"sum": expected})
        sum_cells = locate_sum(item)
        detail = {
            "expected_sum": expected,
            "c1_value": "none" if actual is None else actual,
            "sum_found_at": sum_cells or "nowhere",
        }
        return ItemResult(item.id, ok=False, detail=detail)

    return Claim(
        statement="Every workbook's total in C1 equals the sum of column A",
        predicate=predicate,
    )

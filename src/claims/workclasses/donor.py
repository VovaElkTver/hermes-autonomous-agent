"""Work class 2 — donor analysis with coverage by trace.

Population = every source file in a donor repo/dir (the full set of files to
be "разобран"). The candidate (a donor-analysis agent) reads only *some* of
the files and then asserts something that is supposed to hold for *all* of
them. The harness re-runs the candidate's predicate over the FULL population
(computed from the files on disk, not from what the agent read) and measures
coverage from the agent's trace.

This is the class that exposes silent population substitution: if the agent
only read 10 of 15 files, the harness still checks all 15 and reports the
counterexamples hiding in the 5 unread files.
"""

from __future__ import annotations

import os
import re
from typing import List

from ..core import Claim, Item, ItemResult, Population

# A top-level ``def main(...)`` — not indented, not a nested method.
_MAIN_RE = re.compile(r"^[ \t]{0}def main\s*\(")


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def defines_main(item: Item) -> bool:
    text: str = item.payload
    for line in text.splitlines():
        if _MAIN_RE.match(line):
            return True
    return False


def enumerate_population(directory: str, suffix: str = ".py") -> Population:
    """Harness-side enumeration of all source files under ``directory``."""
    items: List[Item] = []
    for root, _dirs, files in os.walk(directory):
        for name in sorted(files):
            if not name.endswith(suffix):
                continue
            full = os.path.join(root, name)
            rel = os.path.relpath(full, directory)
            items.append(Item(id=rel, label=rel, payload=_read(full)))
    items.sort(key=lambda i: i.id)
    return Population(name="donor-source-files", source=directory, items=items)


def build_entry_point_claim() -> Claim:
    """Candidate's assertion: every source file exposes a top-level main()."""

    def predicate(item: Item) -> ItemResult:
        if defines_main(item):
            return ItemResult(item.id, ok=True, detail={"entry_point": "main"})
        return ItemResult(item.id, ok=False, detail={"reason": "no top-level def main"})

    return Claim(
        statement="Every source file defines a top-level main() entry point",
        predicate=predicate,
    )

"""The single clock the processor reasons about.

Absolute system time is the source of truth (vovak 26.10). ``Clock`` is a
thin seam so tests and the gateway can pin/observe ``now()`` without monkey-
patching the whole stdlib; nothing else may smuggle a different clock into
predicates.
"""

from __future__ import annotations

import time
from typing import Callable


class Clock:
    def __init__(self, source: Callable[[], float] | None = None) -> None:
        self._source = source or time.time

    def now(self) -> float:
        return self._source()


_default = Clock()


def now() -> float:
    """Module-level alias: absolute epoch seconds from the shared clock."""
    return _default.now()

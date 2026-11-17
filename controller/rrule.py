"""The stdlib-only RRULE core for ``ctl_followup`` (TZ-2026-11-16-dsh-B, B4).

The carrier stays the event row: the ticker re-plates a follow-up row that
carries an ``rrule`` to its next occurrence (the row id is stable — no
delete+insert). This module is the deterministic half of that:

* ``parse_rrule`` — the mini-grammar: ``FREQ`` DAILY|WEEKLY|MONTHLY,
  optional ``INTERVAL``, optional ``BYDAY`` (a WEEKLY-only weekday
  selector, MO/WE/... for «review on Monday/Wednesday, not every N
  seconds»). No ``COUNT``: the end of a series is a row revocation (or
  ``until_ts``, when there is one), so COUNT is rejected explicitly
  rather than silently dropped.
* ``next_due_at`` — the first occurrence strictly after *from_ts*,
  keeping the anchor's UTC time-of-day (the RFC 5545 DTSTART role).

All arithmetic is epoch-seconds + UTC (DST-free): the rows in state.db
carry REAL epochs, and a weekly review that means «every Monday 09:00»
must mean the same thing on every host.
"""
from __future__ import annotations

import calendar
import math
import time
from typing import Any

_FREQS = ("DAILY", "WEEKLY", "MONTHLY")
_WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
_DAYNUM = {d: i for i, d in enumerate(_WEEKDAYS)}  # 0 = Monday
_PARTS = ("FREQ", "INTERVAL", "BYDAY")
_DAY_SECONDS = 86_400
#: Search window for cycle-based lookups: 200 cycles covers a year at
#: any interval this carrier will plausibly carry, and keeps the loop
#: bounded.
_MAX_CYCLES = 200


class RRuleError(ValueError):
    """The string is not in this carrier's mini-grammar."""


def parse_rrule(value: str) -> dict[str, Any]:
    """Parse the mini-grammar into ``{"freq", "interval", "byday"}``.

    The optional leading ``RRULE:`` token is accepted (the field in the
    row may carry either the bare body or the full RFC 5545 form).
    Unknown parts — including ``COUNT`` — raise :class:`RRuleError`:
    the series end is a row revocation (or ``until_ts``), not a count.
    """
    s = (value or "").strip().upper()
    s = s.removeprefix("RRULE:")
    if not s:
        raise RRuleError("empty rrule")
    out: dict[str, Any] = {"freq": "WEEKLY", "interval": 1, "byday": ()}
    for part in s.split(";"):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise RRuleError(f"malformed rrule part {part!r}")
        key, _, raw = part.partition("=")
        key, raw = key.strip(), raw.strip()
        if key not in _PARTS:
            raise RRuleError(
                f"unsupported rrule part {key!r}: this carrier supports only "
                + "/".join(_PARTS)
                + " — a series ends by revoking the row (or until_ts), not COUNT")
        if key == "FREQ":
            if raw not in _FREQS:
                raise RRuleError(f"unsupported FREQ {raw!r}: want one of {'/'.join(_FREQS)}")
            out["freq"] = raw
        elif key == "INTERVAL":
            try:
                iv = int(raw)
            except ValueError:
                raise RRuleError(f"INTERVAL must be an integer, got {raw!r}") from None
            if iv < 1:
                raise RRuleError("INTERVAL must be >= 1")
            out["interval"] = iv
        else:  # BYDAY
            days = [d.strip().upper() for d in raw.split(",") if d.strip()]
            if not days:
                raise RRuleError("BYDAY must name at least one weekday")
            for d in days:
                if d not in _DAYNUM:
                    raise RRuleError(f"unknown weekday {d!r}: want one of {', '.join(_WEEKDAYS)}")
            out["byday"] = tuple(sorted(set(_DAYNUM[d] for d in days)))
    if out["byday"] and out["freq"] != "WEEKLY":
        raise RRuleError("BYDAY is a WEEKLY-only selector in this carrier")
    return out


def _anchor_day_and_tod(anchor: float) -> tuple[float, float]:
    day = math.floor(anchor / _DAY_SECONDS)
    return day, anchor - day * _DAY_SECONDS


def next_due_at(rrule: str, from_ts: float, anchor_ts: float | None = None) -> float:
    """The first occurrence strictly after *from_ts* (epoch seconds).

    The occurrences keep the anchor's UTC time-of-day; *anchor_ts* is the
    reference the series counts from (the row's first ``due_at`` at
    creation, or a re-plated ``due_at`` — a periodic series is closed
    under «the next after an occurrence», so re-anchoring on the last
    occurrence does not drift it).
    """
    spec = parse_rrule(rrule)
    anchor = anchor_ts if anchor_ts is not None else from_ts
    freq, interval, byday = spec["freq"], spec["interval"], spec["byday"]
    day, tod = _anchor_day_and_tod(anchor)

    if freq == "DAILY":
        step = _DAY_SECONDS * interval
        base = day * _DAY_SECONDS + tod
        k = math.ceil((from_ts - base) / step)
        cand = base + k * step
        if cand <= from_ts:
            cand += step
        return cand

    if freq == "WEEKLY":
        weekday = time.gmtime(anchor).tm_wday  # 0 = Monday
        monday = day - weekday
        offsets = byday if byday else (weekday,)
        best: float | None = None
        for k in range(_MAX_CYCLES):
            for off in offsets:
                cand = (monday + off + 7 * interval * k) * _DAY_SECONDS + tod
                if cand > from_ts and (best is None or cand < best):
                    best = cand
        if best is None:
            raise RRuleError("no occurrence within the search window; the series is stale")
        return best

    # MONTHLY: the anchor's day-of-month, stepped by interval months; a
    # month that lacks the day (31st in February) is skipped, not clamped.
    gm = time.gmtime(anchor)
    dom, y, m = gm.tm_mday, gm.tm_year, gm.tm_mon
    for k in range(_MAX_CYCLES):
        yy, mm = y, m + interval * k
        while mm > 12:
            mm -= 12
            yy += 1
        if dom <= calendar.monthrange(yy, mm)[1]:
            cand = calendar.timegm((yy, mm, dom, 0, 0, 0, 0, 0)) + tod
            if cand > from_ts:
                return cand
    raise RRuleError("no occurrence within the search window; the series is stale")

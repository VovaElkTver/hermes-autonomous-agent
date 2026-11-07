#!/usr/bin/env python3
"""Fire a tasks_kernel event from outside the gateway — the cron replacement.

Usage: task_emit.py <event_id> [--db PATH]

Designed for systemd timers / .path units (no-agent producers): the timer is an
external event source, NOT an agent turn. The script imports tasks_kernel from
the repo checkout next to it, wakes every wait_event subscriber of <event_id>
(deterministic hermes-send delivery, no LLM) and prints a one-line result.

Exit codes: 0 = event fired (resumed N printed), 2 = bad usage, 1 = error.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    ap = argparse.ArgumentParser(description="Wake tasks_kernel wait_event subscribers")
    ap.add_argument("event_id")
    ap.add_argument("--db", default=None, help="state.db path (default: HERMES_STATE_DB or ~/.hermes/state.db)")
    args = ap.parse_args()

    import tasks_kernel

    resumed = tasks_kernel.emit(args.event_id, db_path=args.db)
    print(f"event {args.event_id}: resumed {resumed}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # fail visible for the timer journal
        print(f"task_emit error: {exc}", file=sys.stderr)
        sys.exit(1)

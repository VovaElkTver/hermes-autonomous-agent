"""S2 (TZ-memdoor-5090-v2): ``hermes savings`` — the savings-ledger summary.

Prints what the ledger recorded: MB that did not enter context, ≈tokens
(bytes/4), and the ≈$ floor when a cheapest-model price is supplied.
Machine-readable with ``--json``.
"""

from __future__ import annotations

import json

from tools import savings_ledger


def cmd_savings(args) -> int:
    s = savings_ledger.summarize(
        month=getattr(args, "month", None),
        price_usd_per_mtok=getattr(args, "price_per_mtok", None),
    )
    if getattr(args, "json", False):
        print(json.dumps(s, ensure_ascii=False, indent=2))
        return 0
    print(f"savings ledger: {savings_ledger.ledger_path()}")
    if s["month"]:
        print(f"month:           {s['month']} (UTC)")
    print(f"events:            {s['events']}")
    print(f"raw (original):    {s['raw_b']:,} B")
    print(f"kept (in context): {s['kept_b']:,} B")
    print(f"not-in-context:    {s['saved_b']:,} B  ({s['saved_mb']} MB)")
    print(f"≈ tokens (b/4):    {s['approx_tokens']:,}")
    if s["approx_usd_floor"] is not None:
        print(f"≈ $ floor:         ${s['approx_usd_floor']:.4f} (cheapest-model floor)")
    else:
        print("≈ $ floor:         n/a — pass --price-per-mtok US/Mtok (cheapest model of the month)")
    if s["by_tool_saved_b"]:
        print("by tool:")
        for tool, b in sorted(s["by_tool_saved_b"].items(), key=lambda kv: -kv[1]):
            print(f"  {tool:<16} {b:,} B not in context")
    return 0


def build_parser(subparsers) -> None:
    p = subparsers.add_parser(
        "savings",
        help="summarize the savings ledger (~/.hermes/savings.jsonl)",
    )
    p.add_argument("--month", help="restrict to a UTC month, YYYY-MM")
    p.add_argument("--price-per-mtok", dest="price_per_mtok", type=float,
                   help="cheapest model $/M tokens for the month → ≈$ floor")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_savings)

#!/usr/bin/env python3
"""Drop write-approval review i18n keys (gate retired; memory/skill writes are direct)."""
import re
from pathlib import Path

LOCALES = Path(__file__).resolve().parent.parent / "locales"


def main() -> None:
    total = 0
    for f in sorted(LOCALES.glob("*.yaml")):
        out, sec, sub, removed = [], "", "", 0
        for line in f.read_text(encoding="utf-8").split("\n"):
            mt = re.match(r"^([a-z_][a-z0-9_]*):\s*$", line)
            ms = re.match(r"^  ([a-z_][a-z0-9_]*):\s*(#.*)?$", line)
            if mt:
                sec, sub = mt.group(1), ""
            elif ms and sec:
                sub = ms.group(1)
            mk = re.match(r"^\s+(gate_off|diff_truncated|unknown_subcommand):\s", line)
            if mk and (
                (sec == "gateway" and sub in ("skills", "memory"))
                or (sec == "cli" and sub == "memory")
            ):
                removed += 1
                continue
            out.append(line)
        if removed:
            f.write_text("\n".join(out), encoding="utf-8")
            total += removed
            print(f"{f.name}: -{removed}")
    print(f"total: {total}")


if __name__ == "__main__":
     main()

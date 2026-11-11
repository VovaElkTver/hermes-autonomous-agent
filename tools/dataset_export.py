"""Trajectory exporter: session messages → ShareGPT training conversations.

Contract donor: camel-ai/camel data_collectors (Apache-2.0) — mapping tables and
validity rules ported clean-room to our durable storage (STEAL-PLAN item 1).

ShareGPT item shape (camel data_collectors/conversation_models.py semantics):
    {"system": str|absent, "tools": JSON-str|absent,
     "conversations": [{"from": human|gpt|function_call|observation, "value": str}]}
Invariants enforced (conversation_models.py:63-77 analog):
    - conversations non-empty; first item is system-wrapped human after optional system;
    - an `observation` may follow only a `gpt` item that carried a function_call;
    - any single value ≤ MAX_VALUE_CHARS chars.

Read-only over $HERMES_HOME/state.db. Nothing here writes agent state.
"""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import json
import pathlib
import sqlite3
import sys

MAX_VALUE_CHARS = 100_000
TOOL_ROLES = ("function", "tool")
DEDUP_THRESHOLD = 0.65        # steal-plan №8: donor default similarity cut-off
DEDUP_COMPARE_CHARS = 4000    # similarity prefix budget (O(n²) guard)


def _connect(db_path: pathlib.Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _has_column(con: sqlite3.Connection, table: str, col: str) -> bool:
    return any(r[1] == col for r in con.execute(f"pragma table_info({table})"))


def _iter_sessions(con: sqlite3.Connection, since_epoch: float | None):
    q = "select id, coalesce(system_prompt,'') as sp from sessions"
    args: tuple = ()
    if since_epoch:
        q += " where started_at >= ?"
        args = (since_epoch,)
    for row in con.execute(q + " order by id", args):
        yield row["id"], row["sp"]


def _select_messages(con, cols: str, filters: str, args: tuple):
    q = f"select {cols} from messages {filters} order by session_id, timestamp, id"
    return con.execute(q, args)


def session_to_conversation(
    sys_prompt: str, rows: list[sqlite3.Row], has_flags: bool
) -> dict | None:
    """Build one ShareGPT conversation from ordered message rows of a session."""
    convs: list[dict] = []
    last_gpt_had_call = False
    if sys_prompt:
        convs.append({"from": "system", "value": sys_prompt})
    for r in rows:
        if has_flags and (r["active"] == 0 or r["compacted"] == 1):
            continue  # drop compressed-away duplicates, keep live transcript only
        role, content = r["role"], r["content"] or ""
        calls = json.loads(r["tool_calls"]) if r["tool_calls"] else []
        if role in TOOL_ROLES:
            if not last_gpt_had_call:
                continue  # orphan observation: skip rather than violate invariant
            convs.append({"from": "observation", "value": content[:MAX_VALUE_CHARS]})
            last_gpt_had_call = False
        elif role == "assistant" and calls:
            if content.strip():
                convs.append({"from": "gpt", "value": content[:MAX_VALUE_CHARS]})
            for c in calls:
                fn = (c.get("function") or {})
                payload = {"name": fn.get("name"), "arguments": fn.get("arguments")}
                convs.append({
                    "from": "function_call",
                    "value": json.dumps(payload, ensure_ascii=False)[:MAX_VALUE_CHARS],
                })
            last_gpt_had_call = True
        elif role == "assistant":
            if content.strip():
                convs.append({"from": "gpt", "value": content[:MAX_VALUE_CHARS]})
                last_gpt_had_call = False
        elif role == "user":
            if content.strip():
                convs.append({"from": "human", "value": content[:MAX_VALUE_CHARS]})
                last_gpt_had_call = False
    # strip trailing dangling call whose observation never came
    while convs and convs[-1]["from"] in ("function_call",):
        convs.pop()
    # merge consecutive same-role items (e.g. multiple user messages in a row)
    merged: list[dict] = []
    for c in convs:
        if merged and merged[-1]["from"] == c["from"]:
            merged[-1]["value"] = (merged[-1]["value"] + "\n\n" + c["value"])[:MAX_VALUE_CHARS]
        else:
            merged.append(c)
    heads = [c["from"] for c in merged]
    if len(heads) < 2 or "human" not in heads or "gpt" not in heads:
        return None  # not a usable trajectory
    return {"conversations": merged}


def export_sharegpt(
    db_path: pathlib.Path, since_epoch: float | None = None, limit: int | None = None
) -> list[dict]:
    con = _connect(db_path)
    out: list[dict] = []
    has_flags = _has_column(con, "messages", "active") and _has_column(
        con, "messages", "compacted"
    )
    cols = "session_id, role, content, tool_calls" + (
        ", active, compacted" if has_flags else ""
    )
    filters = (
        "where timestamp >= ?" if since_epoch else ""
    )
    args: tuple = (since_epoch,) if since_epoch else ()
    buckets: dict[str, list] = {}
    sp_map: dict[str, str] = {}
    for sid, sp in _iter_sessions(con, since_epoch):
        sp_map[sid] = sp
    for row in _select_messages(con, cols, filters, args):
        buckets.setdefault(row["session_id"], []).append(row)
    con.close()
    for sid, rows in buckets.items():
        conv = session_to_conversation(sp_map.get(sid, ""), rows, has_flags)
        if conv is not None:
            out.append({"id": sid, **conv})
            if limit and len(out) >= limit:
                break
    return out


def validate(conv: dict) -> bool:
    """Same three invariants camel enforces in conversation_models.py."""
    cs = conv["conversations"]
    bodies = [c for c in cs if c["from"] != "system"]
    if not cs or not bodies:
        return False
    if any(len(c["value"]) > MAX_VALUE_CHARS for c in cs):
        return False
    prev_from = cs[0]["from"]
    if prev_from not in ("system", "human"):
        return False
    awaiting_observation = False
    for c in cs:
        f = c["from"]
        if f == "observation" and not awaiting_observation:
            return False
        if f == "function_call":
            awaiting_observation = True
        elif f in ("human", "gpt"):
            awaiting_observation = False
    return True


def deduplicate(items: list[dict], threshold: float = DEDUP_THRESHOLD) -> tuple[list[dict], int]:
    """Near-duplicate removal (clean-room contract of camel deduplicate_internally).

    Similarity = difflib ratio over the first DEDUP_COMPARE_CHARS of the
    concatenated message values; the FIRST occurrence is kept. Default off —
    enabled by --dedup, mirroring the donor's opt-in semantics.
    """
    def _text(it: dict) -> str:
        return "\n".join(c["value"] for c in it["conversations"])[:DEDUP_COMPARE_CHARS]

    kept: list[dict] = []
    dropped = 0
    for it in items:
        t = _text(it)
        if any(difflib.SequenceMatcher(None, t, _text(k)).ratio() > threshold for k in kept):
            dropped += 1
            continue
        kept.append(it)
    return kept, dropped


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(pathlib.Path.home() / ".hermes/state.db"))
    ap.add_argument("--since", help="ISO date, e.g. 2026-11-01")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--out", required=True)
    ap.add_argument("--dedup", action="store_true",
                    help=f"drop near-duplicate trajectories (ratio > {DEDUP_THRESHOLD})")
    a = ap.parse_args(argv)
    since_iso = None
    if a.since:
        since_iso = dt.datetime.fromisoformat(a.since).timestamp()
    convs = export_sharegpt(pathlib.Path(a.db), since_iso, a.limit)
    if a.dedup:
        convs, dropped = deduplicate(convs)
        print(f"dedup: dropped {dropped} near-duplicates")
    bad = [c["id"] for c in convs if not validate(c)]
    if bad:
        print(f"invariant violations: {bad[:5]}", file=sys.stderr)
        return 2
    out = pathlib.Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(convs, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(out)
    print(f"{len(convs)} trajectories -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

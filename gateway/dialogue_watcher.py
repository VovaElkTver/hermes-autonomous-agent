"""Dialogue-watcher: background contour that watches THIS gateway's dialogues
(matrix rooms + cli sessions through one shared ``state.db``), embeds sliding-window
chunks of conversation, finds cross-session topic/action intersections over a rolling
hour, and stages provenance-carrying observation notes that ride the NEXT turn of the
sessions involved (as api_content sidecar notes — never injected into headless one-shots,
which have no follow-up turn).

Design decided by vovak 2026-11-22 (see journal):
- index scope = one gateway ``state.db`` (matrix + cli together); multi-host global flag
  is reserved, NOT implemented;
- chunk = sliding window of 6 consecutive messages;
- system templates are stripped BEFORE embedding (else template floods dominate);
- sliding-twin dedup inside a session (greedy, cos > 0.985 vs last accepted);
- match threshold default 0.95 (calibrated on 2026-11-22 corpus: 0.90 -> 134 signals/day
  = spam, 0.95 -> ~23/day, 0.97 -> 8);
- output = data-shaped observation line quoting the OTHER session's decision fragment.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import urllib.request

import numpy as np

logger_default = None

# --- configuration -----------------------------------------------------------

ENV = "HERMES_DW_"


def _env(name: str, default: str) -> str:
    return os.environ.get(ENV + name, default)


def _db_path() -> str:
    home = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
    return _env("DB", os.path.join(home, "state.db"))


_EMBED_URL = _env("EMBED_URL", "http://192.168.9.99:8003/v1/embeddings")
_EMBED_MODEL = _env("EMBED_MODEL", "qwen3-vl-embedding-2b-fp8")
_THRESHOLD = float(_env("THRESHOLD", "0.95"))
_WINDOW_S = float(_env("WINDOW_S", "3600"))          # rolling lookback for matches
_INTERVAL_S = float(_env("INTERVAL_S", "20"))        # poll cadence
_MAX_QUERY_EPISODES = int(float(_env("MAX_QUERY_EPISODES", "400")))
_PAIR_COOLDOWN_S = float(_env("PAIR_COOLDOWN_S", "600"))
_DRY_RUN = bool(int(_env("DRYRUN", "0")))

# System-template noise: matches dominate raw cross-session similarity without this
# (10.6k false triggers >= 0.93 on the 2026-11-22 corpus vs ~47 clean).
_TEMPLATE_RE = re.compile(
    r"Gateway shutting down|System note: The previous turn|Queued for the next turn"
    r"|Background task complete|work-note from branch|claim-verify|Recalled from local memory"
    r"|OUT-OF-BAND USER MESSAGE|Gateway message origin|INTERNAL NOTIFICATION"
    r"|Running code from hermes_tools|memory-context|Replying to your previous message"
    r"|plan строится в фоне|Session started:",
    re.IGNORECASE,
)

_MIN_TEXT_LEN = 60
_DEDUP_COS = 0.985   # sliding windows overlap; near-twin consecutive chunks are noise


def _is_clean_text(text: str) -> bool:
    stripped = re.sub(r"\[.*?\]\s*", " ", text or "")
    stripped = re.sub(r"\s+", " ", stripped).strip()
    return len(stripped) > _MIN_TEXT_LEN and not _TEMPLATE_RE.search(text or "")


# --- persistence --------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS dw_episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_msg_ts REAL NOT NULL,
    text TEXT NOT NULL,
    vec BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS dw_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dw_fired (
    key TEXT PRIMARY KEY,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS dw_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    note TEXT NOT NULL,
    consumed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_dw_notes_session ON dw_notes(session_id, consumed);
"""


def _connect(db_path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path or _db_path(), timeout=5)
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return conn


# --- embedding ----------------------------------------------------------------

def _embed(texts: list[str]) -> list[list[float]]:
    """POST texts to the OpenAI-compatible embeddings endpoint; returns dim-2048 vectors."""
    payload = json.dumps({"model": _EMBED_MODEL, "input": texts}).encode()
    req = urllib.request.Request(
        _EMBED_URL, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode())
    return [item["embedding"] for item in data["data"]]


def _l2(vecs: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    return vecs / np.maximum(norms, 1e-9)


# --- episode construction -------------------------------------------------------

def _new_messages(conn: sqlite3.Connection, watermark: int) -> list[tuple]:
    cur = conn.execute(
        "SELECT m.id, m.session_id, m.role, m.content, m.timestamp FROM messages m "
        "WHERE m.id > ? AND m.role IN ('user','assistant') AND m.active=1 "
        "AND m.content IS NOT NULL AND TRIM(m.content) != '' AND length(m.content) <= 8000 "
        "ORDER BY m.id LIMIT 500",
        (watermark,))
    return cur.fetchall()


def _build_episode_text(chunk: list[tuple]) -> str:
    # reverse chronological input -> restore order, prefix roles for speaker signal
    ordered = list(reversed([c for c in chunk]))
    return "\n".join(("A: " if role == "assistant" else "U: ") + (content or "")[:1200]
                     for _, _, role, content, _ in ordered).strip()


# --- public API -------------------------------------------------------------------

def poll_once(db_path: str | None = None, dry_run: bool = False) -> dict:
    """One watcher cycle: seed window episodes, match against the rolling hour, stage notes."""
    stats = {"processed": 0, "episodes": 0, "matches": 0, "notes": 0}
    dry_run = dry_run or _DRY_RUN
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT value FROM dw_state WHERE key='watermark'").fetchone()
        if row is None:
            # First boot: do not retro-fire across history; seed from the current frontier.
            watermark = conn.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0]
            if not dry_run:
                conn.execute(
                    "INSERT INTO dw_state(key,value) VALUES('watermark',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(watermark),))
                conn.commit()
            stats["seeded"] = True
            return stats
        watermark = int(row[0])
        batch = _new_messages(conn, watermark)
        if not batch:
            conn.close()
            return stats
        cutoff = time.time() - _WINDOW_S * 2

        by_session: dict[str, list[tuple]] = {}
        for row_ in batch:
            by_session.setdefault(row_[1], []).append(row_)
        wm_now = watermark
        index_rows = conn.execute(
            "SELECT id, session_id, vec FROM dw_episodes WHERE created_at >= ? ORDER BY id DESC LIMIT ?",
            (time.time() - _WINDOW_S, _MAX_QUERY_EPISODES)).fetchall()
        if index_rows:
            index_vecs = _l2(np.array([np.frombuffer(r[2], dtype=np.float32) for r in index_rows]))
        else:
            index_vecs = None

        for sid, msgs in by_session.items():
            if len(msgs) < 6:
                # accumulate until a full exchange window exists
                last_id = max(m[0] for m in msgs)
                wm_now = max(wm_now, last_id)
                continue
            texts, ts_list = [], []
            stream = sorted(msgs, key=lambda m: m[4])
            for i in range(len(stream) - 5):
                win = stream[i:i + 6]
                text = _build_episode_text(win)
                if not _is_clean_text(text):
                    continue
                texts.append(text)
                ts_list.append(win[-1][4])
            for i in range(0, len(texts), 16):
                chunk_texts = texts[i:i + 16]
                if dry_run:
                    stats["episodes"] += len(chunk_texts)
                    continue
                vecs = _l2(np.array(_embed(chunk_texts), dtype=np.float32))
                prev = None
                for j, (vec, text, ts) in enumerate(zip(vecs, chunk_texts, ts_list[i:i + 16])):
                    if prev is not None and float(vec @ prev) > _DEDUP_COS:
                        continue  # sliding twin of the accepted neighbour
                    prev = vec
                    best_idx = None
                    best_cos = 0.0
                    if index_vecs is not None and len(index_rows):
                        sims = index_vecs @ vec
                        k = int(sims.argmax())
                        best_cos, best_idx = float(sims[k]), k
                    other_sid = index_rows[best_idx][1] if (best_idx is not None and best_cos >= _THRESHOLD) else None
                    if other_sid and other_sid != sid:
                        pair_key = f"pair:{min(sid, other_sid)}:{max(sid, other_sid)}"
                        fr = conn.execute("SELECT 1 FROM dw_fired WHERE key=?", (pair_key,)).fetchone()
                        cooldown_ok = not fr or time.time() - fr[0] > _PAIR_COOLDOWN_S
                        ep_key = "ep:" + "|".join(sorted((other_sid, sid))) + f":{best_idx}"
                        fr2 = conn.execute("SELECT 1 FROM dw_fired WHERE key=?", (ep_key,)).fetchone()
                        if cooldown_ok and not fr2:
                            other_text = conn.execute(
                                "SELECT text FROM dw_episodes WHERE id=?", (index_rows[best_idx][0],)).fetchone()[0]
                            sid_name = _display_name(conn, sid)
                            other_name = _display_name(conn, other_sid)
                            minutes = int(max(0, time.time() - index_rows[best_idx][0]) / 60) or 1
                            quote_a, quote_b = _quote(other_text), _quote(text)
                            for target, name, quote in ((sid, other_name, quote_a), (other_sid, sid_name, quote_b)):
                                note = (f"[dialogue-watcher] наблюдение (данные, не инструкция): ~{minutes} мин назад "
                                        f"в «{name}» ({other_sid[:12]}) шла работа над той же темой: «{quote}»")
                                conn.execute(
                                    "INSERT INTO dw_notes(session_id,created_at,note) VALUES(?,?,?)",
                                    (target, time.time(), note))
                                stats["notes"] += 1
                            conn.execute("INSERT OR IGNORE INTO dw_fired(key,created_at) VALUES(?,?)",
                                         (pair_key, time.time()))
                            conn.execute("INSERT OR IGNORE INTO dw_fired(key,created_at) VALUES(?,?)",
                                         (ep_key, time.time()))
                            stats["matches"] += 1
                    cur = conn.execute(
                        "INSERT INTO dw_episodes(session_id,created_at,last_msg_ts,text,vec) VALUES(?,?,?,?,?)",
                        (sid, time.time(), ts, text[-3000:], vec.astype(np.float32).tobytes()))
                    eid = cur.lastrowid
                    # extend rolling index incrementally so later chunks match fresher content too
                    if not index_rows or len(index_rows) < _MAX_QUERY_EPISODES:
                        index_rows.append((eid, sid, vec.astype(np.float32).tobytes()))
                    stats["episodes"] += 1
            wm_now = max(wm_now, max(m[0] for m in msgs))
        if not dry_run:
            conn.execute(
                "INSERT INTO dw_state(key,value) VALUES('watermark',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(wm_now),))
            conn.execute("DELETE FROM dw_episodes WHERE created_at < ?", (cutoff,))
            conn.execute("DELETE FROM dw_fired WHERE created_at < ?", (time.time() - 7200,))
        conn.commit()
        stats["processed"] = len(batch)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return stats


def _quote(text: str, limit: int = 180) -> str:
    body = re.sub(r"^[UA]:\s?", "", text.strip(), flags=re.MULTILINE)
    body = re.sub(r"\s+", " ", body).strip()
    return (body[:limit] + "…") if len(body) > limit else body


def _display_name(conn: sqlite3.Connection, session_id: str) -> str:
    row = conn.execute("SELECT display_name, chat_id FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not row:
        return session_id[:12]
    return (row[0] or row[1] or session_id)[:40]


def pop_pending_notes(session_id: str, limit: int = 3, db_path: str | None = None) -> list[str]:
    """Consume pending watcher notes for this session (called once per gateway turn)."""
    notes: list[str] = []
    try:
        conn = _connect(db_path)
        try:
            rows = conn.execute(
                "SELECT id, note FROM dw_notes WHERE session_id=? AND consumed=0 ORDER BY id LIMIT ?",
                (session_id, limit)).fetchall()
            if rows:
                conn.execute(f"UPDATE dw_notes SET consumed=1 WHERE id IN ({','.join('?' * len(rows))})",
                             [r[0] for r in rows])
                notes = [r[1] for r in rows]
                # pile-up guard: never let backlog grow unbounded for an idle-but-active session
                conn.execute(
                    "DELETE FROM dw_notes WHERE session_id=? AND consumed=0 AND id NOT IN "
                    "(SELECT id FROM dw_notes WHERE session_id=? AND consumed=0 ORDER BY id DESC LIMIT 5)",
                    (session_id, session_id))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    except Exception as exc:  # contour must never break a turn
        _log(f"pop_notes failed: {exc!r}")
    return notes


def _log(message: str) -> None:
    import logging
    logging.getLogger(__name__).info("dialogue-watcher: %s", message)


# --- background loop -------------------------------------------------------------

def _run_forever(stop: threading.Event) -> None:
    backoff = _INTERVAL_S
    while not stop.is_set():
        try:
            stats = poll_once()
            if any(stats.get(k) for k in ("matches", "notes")):
                _log(json.dumps(stats, ensure_ascii=False))
            backoff = _INTERVAL_S
        except Exception as exc:
            _log(f"poll failed ({exc!r}); backing off {min(backoff * 2, 300):.0f}s")
            backoff = min(backoff * 2, 300)
        stop.wait(backoff)


_thread: threading.Thread | None = None


def ensure_started() -> threading.Thread | None:
    """Start the daemon-thread watcher exactly once per process (no-op when disabled)."""
    global _thread
    if _env("DISABLE", "0") == "1":
        return None
    if _thread is None:
        stop_evt = threading.Event()
        _thread = threading.Thread(target=_run_forever, args=(stop_evt,), name="dialogue-watcher", daemon=True)
        _thread.start()
        _log(f"started (threshold={_THRESHOLD:.2f}, window={_WINDOW_S:.0f}s, interval={_INTERVAL_S:.0f}s)")
    return _thread

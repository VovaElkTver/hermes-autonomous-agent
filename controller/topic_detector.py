"""controller.topic_detector — stage003 step 7: implicit-topic detector.

Observer only. Controller topics are DECLARED (ctl_task contracts); this
module exists for interactive conversations outside the controller and only
GUESSES which topic a user message belongs to, recording the guess in
ctl_topic_detect. Nothing reads it back into behaviour until Вл's acceptance
(same discipline as step 6's shadow ledger): the detector never changes a
prompt, never retries a turn, never blocks the turn — it runs on its own
thread and is fail-open everywhere.

Corpus = active declared task goals (ctl_task) + previously proposed topic
snippets (ctl_topic_detect.proposed_new). Matching = cosine over the same
local embedder as the sqlite memory plugin (plugins.sqlite.embedding_url);
lexical token-coverage is the fallback when the endpoint is down, so shadow
data keeps accruing. ``method`` is recorded per row so acceptance can tell
the evidence classes apart.

Config gate ``memory.topic_detect_enabled`` defaults to False: zero DB
writes, zero HTTP, zero behaviour — the step-4 «без env — ноль байт» rule.
"""
from __future__ import annotations

import hashlib
import logging
import math
import re
import time

logger = logging.getLogger(__name__)

# Terminal-ish states: a finished/cancelled task's goal is no longer a live
# topic candidate for an ongoing conversation.
_CLOSED_STATES = ("done", "failed", "cancelled", "closed")

_EMBED_CACHE: dict[str, list[float]] = {}
_EMBED_CACHE_MAX = 512

_WORD_RE = re.compile(r"\w+", re.UNICODE)


def enabled(config: dict | None = None) -> bool:
    """Config flag (memory.topic_detect_enabled, default False). Fail-open to
    OFF: config trouble must leave the observer inert, never half-on."""
    try:
        if config is None:
            from hermes_cli.config import load_config_readonly
            config = load_config_readonly() or {}
        mem = config.get("memory") if isinstance(config, dict) and isinstance(config.get("memory"), dict) else {}
        return bool(mem.get("topic_detect_enabled", False))
    except Exception:
        return False


def _mem_cfg(config: dict | None, key: str, default):
    try:
        if config is None:
            from hermes_cli.config import load_config_readonly
            config = load_config_readonly() or {}
        mem = config.get("memory") if isinstance(config, dict) else {}
        return (mem or {}).get(key, default)
    except Exception:
        return default


def _plugin_cfg(config: dict | None, key: str, default):
    try:
        if config is None:
            from hermes_cli.config import load_config_readonly
            config = load_config_readonly() or {}
        plugins = (config or {}).get("plugins") or {}
        sqlite_cfg = plugins.get("sqlite") or {}
        return sqlite_cfg.get(key, default)
    except Exception:
        return default


def _excerpt(text: str, limit: int = 120) -> str:
    return " ".join(str(text).split())[:limit]


def _tokens(text: str) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(text or "") if len(w) >= 3}


def _coverage(message_tokens: set[str], topic_tokens: set[str]) -> float:
    """Share of message tokens present in the topic text. Asymmetric by
    design: a short message against a long goal makes Jaccard useless."""
    if not message_tokens or not topic_tokens:
        return 0.0
    return len(message_tokens & topic_tokens) / len(message_tokens)


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / (na * nb)


def _embed(text: str, config: dict | None) -> list[float] | None:
    """One embedding via the memory plugin's endpoint. Any trouble -> None
    (the caller falls back to lexical). Never raises."""
    key = hashlib.sha256(text.encode("utf-8")).hexdigest()
    cached = _EMBED_CACHE.get(key)
    if cached is not None:
        return cached
    url = str(_plugin_cfg(config, "embedding_url", "http://192.168.9.100:8003/v1/embeddings"))
    model = str(_plugin_cfg(config, "embedding_model", "qwen3-vl-embedding-2b-fp8"))
    try:
        import httpx
        r = httpx.post(url, json={"model": model, "input": text[:4000]}, timeout=5)
        r.raise_for_status()
        vec = r.json()["data"][0]["embedding"]
        if not isinstance(vec, list) or not vec:
            return None
        vec = [float(x) for x in vec]
        if len(_EMBED_CACHE) >= _EMBED_CACHE_MAX:
            _EMBED_CACHE.clear()
        _EMBED_CACHE[key] = vec
        return vec
    except Exception as exc:
        logger.debug("topic_detector embed unavailable: %s", exc)
        return None


def _corpus(conn, *, max_tasks: int = 30, max_proposals: int = 20):
    """[(label, scope_ref_or_None, provenance)] — live task goals first,
    then previously proposed implicit topics."""
    out: list[tuple[str, str | None, str]] = []
    rows = conn.execute(
        "SELECT task_id, goal FROM ctl_task WHERE state NOT IN (?,?,?,?,?)"
        " ORDER BY updated_at DESC LIMIT ?",
        ("done", "failed", "cancelled", "closed", "archived", max_tasks),
    ).fetchall()
    for row in rows:
        goal = (row["goal"] or "").strip()
        if goal:
            out.append((_excerpt(goal, 300), row["task_id"], "ctl_task"))
    try:
        rows = conn.execute(
            "SELECT DISTINCT proposed_new FROM ctl_topic_detect"
            " WHERE proposed_new IS NOT NULL AND proposed_new <> ''"
            " ORDER BY created_at DESC LIMIT ?",
            (max_proposals,),
        ).fetchall()
        for row in rows:
            out.append((row["proposed_new"], None, "proposed"))
    except Exception as exc:  # table missing on an old DB: tasks-only corpus
        logger.debug("topic_detector proposals skipped: %s", exc)
    return out


def detect(session_id: str, text: str, *, conn=None, config: dict | None = None) -> dict | None:
    """Observe one interactive user turn: classify it against the topic
    corpus and append one ctl_topic_detect row. Never raises, returns the
    recorded row (or None when inert/failed)."""
    if not enabled(config):
        return None
    text = str(text or "").strip()
    if not text:
        return None
    connection = conn
    owns = False
    try:
        if connection is None:
            from controller import model as ctl_model
            connection = ctl_model.connect()
            owns = True
        corpus = _corpus(connection)
        embed_min = float(_mem_cfg(config, "topic_detect_match_min", 0.45))
        lex_min = float(_mem_cfg(config, "topic_detect_lexical_min", 0.25))

        best_label, best_scope = "", None
        best_score, method = 0.0, "skip"
        msg_vec = _embed(text, config) if corpus else None
        if msg_vec is not None:
            method = "embed"
            for label, scope_ref, provenance in corpus:
                vec = _embed(label, config)
                if vec is None or len(vec) != len(msg_vec):
                    continue
                score = _cosine(msg_vec, vec)
                if score > best_score:
                    best_label, best_scope, best_score = label, scope_ref, score
            matched = best_score >= embed_min
        else:
            method = "lexical" if corpus else "skip"
            msg_tokens = _tokens(text)
            for label, scope_ref, provenance in corpus:
                score = _coverage(msg_tokens, _tokens(label))
                if score > best_score:
                    best_label, best_scope, best_score = label, scope_ref, score
            matched = method == "lexical" and best_score >= lex_min

        # Best candidate is always recorded (sub-threshold evidence stays
        # readable for acceptance); proposed_new fires only on no-match.
        proposed = None if matched else _excerpt(text)

        row = {
            "session_id": session_id,
            "message_excerpt": _excerpt(text),
            "best_scope_id": best_scope,
            "best_label": best_label,
            "score": round(float(best_score), 6),
            "method": method,
            "proposed_new": proposed,
            "corpus_size": len(corpus),
        }
        connection.execute(
            "INSERT INTO ctl_topic_detect (detect_id,session_id,message_excerpt,"
            " best_scope_id,best_label,score,method,proposed_new,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (f"ctdet-{time.time_ns():016x}", row["session_id"], row["message_excerpt"],
             row["best_scope_id"], row["best_label"], row["score"], row["method"],
             row["proposed_new"], time.time()),
        )
        connection.commit()
        return row
    except Exception as exc:
        logger.debug("topic_detector skipped: %s", exc)
        return None
    finally:
        if owns and connection is not None:
            try:
                connection.close()
            except Exception:
                pass


def counts(conn, since: float = 0.0) -> dict[str, int]:
    """Per-method counters since a timestamp — acceptance-visible volume of
    detector traffic (always all keys, mirrors topic_memory_shadow.counts)."""
    out = {"embed": 0, "lexical": 0, "skip": 0}
    for row in conn.execute(
        "SELECT method, COUNT(*) AS n FROM ctl_topic_detect WHERE created_at>=?"
        " GROUP BY method", (since,)).fetchall():
        if row["method"] in out:
            out[row["method"]] = row["n"]
    return out

"""Local SQLite semantic memory provider for Hermes Agent.

Successor of the memgraph provider (server retired): facts in a per-profile
SQLite file, temporal (valid_at / invalid_at), embedded via the same local
OpenAI-compatible endpoint, recalled by brute-force cosine over float32 blobs
(1000s of facts — no ANN index needed). Entity graph preserved as plain tables.
When the embedding endpoint is down, writes still succeed and recall falls
back to FTS5 text search; facts without vectors stay searchable by FTS.

DB: $HERMES_HOME/memory.sqlite (resolved per call through get_hermes_home();
never hardcode ~/.hermes — profiles would collide).

Config in $HERMES_HOME/config.yaml:
  plugins:
    sqlite:
      embedding_url: http://192.168.9.100:8003/v1/embeddings
      embedding_model: qwen3-vl-embedding-2b-fp8
      recall_top_k: 5
      min_score: 0.35           # cosine DISTANCE ceiling
      migrate_from: path to a legacy graph.sqlite (MemFact/MemEntity export)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import struct
import threading
import uuid
from datetime import datetime, timezone, UTC
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import (PRE_COMPRESS_CHECKPOINT_API_VERSION, MemoryProvider,
                           spawn_context_thread)

logger = logging.getLogger(__name__)

MEMORY_TOOL_SCHEMA = {
    "name": "sqlite_memory",
    "description": (
        "Local semantic memory (SQLite graph + vector recall).\n"
        "• add — store a durable fact (auto-embeds, links entities).\n"
        "• search — semantic search over stored facts (paraphrase-safe).\n"
        "• related — facts connected to an entity name.\n"
        "• invalidate — mark a fact no longer true (temporal, not deleted).\n"
        "• stats — store health.\n"
        "Use for long-tail knowledge; built-in memory stays for always-on context."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["add", "search", "related", "invalidate", "stats"],
            },
            "text": {"type": "string", "description": "Fact text (add) or query (search)."},
            "entity": {"type": "string", "description": "Entity name for 'related', or the fact's subject for 'invalidate'."},
            "fact_id": {"type": "string", "description": "Fact id for 'invalidate' (from add/search results)."},
            "kind": {
                "type": "string",
                "enum": ["fact", "preference", "decision", "event", "procedure"],
                "description": "Fact category (add; default 'fact').",
            },
            "entities": {
                "type": "array", "items": {"type": "string"},
                "description": "Entity names this fact is about (add).",
            },
            "limit": {"type": "integer", "description": "Max results (default 5)."},
        },
        "required": ["action"],
    },
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts(
  id TEXT PRIMARY KEY, text TEXT NOT NULL, kind TEXT DEFAULT 'fact',
  source_profile TEXT, author TEXT,
  created_at TEXT, valid_at TEXT, invalid_at TEXT,
  embedding BLOB);
CREATE TABLE IF NOT EXISTS entities(name TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS fact_entities(fact_id TEXT, name TEXT, PRIMARY KEY(fact_id, name));
CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(text, content='facts', content_rowid='rowid');
CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
  INSERT INTO facts_fts(rowid, text) VALUES (new.rowid, new.text); END;
CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
  INSERT INTO facts_fts(facts_fts, rowid, text) VALUES('delete', old.rowid, old.text); END;
CREATE INDEX IF NOT EXISTS ix_fe_name ON fact_entities(name);
CREATE TABLE IF NOT EXISTS checkpoints(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT, profile TEXT, ts TEXT NOT NULL,
  sha TEXT NOT NULL, n_messages INTEGER NOT NULL, payload TEXT NOT NULL);
"""


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def _unpack(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))


class SqliteMemoryProvider(MemoryProvider):
    """Temporal fact store in a local SQLite file with vector + FTS recall.

    Opted into the pre-compress checkpoint API v2: ``on_pre_compress`` writes a
    durable, verified snapshot of the evidence about to be compressed; with
    ``require_checkpoint=True`` any durability failure propagates (fail-closed).
    """

    pre_compress_checkpoint_api_version = PRE_COMPRESS_CHECKPOINT_API_VERSION

    def __init__(self, config: dict | None = None):
        self._config = config or {}
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._conn_path = ""
        self._profile = ""
        self._prefetch_cache: dict[str, str] = {}
        self._last_recall_count = 0

    # -- identity / availability ---------------------------------------------

    @property
    def name(self) -> str:
        return "sqlite"

    def _db_path(self) -> Path:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home()) / "memory.sqlite"

    def is_available(self) -> bool:
        # sqlite3 + json are stdlib; embeddings are optional (FTS fallback).
        return True

    def unavailable_reason(self) -> str:
        return ""

    # -- config ---------------------------------------------------------------

    _PLUGIN_KEY = "sqlite"

    def _cfg(self, key: str, default: Any) -> Any:
        try:
            from hermes_cli.config import load_config_readonly
            mine = (load_config_readonly().get("plugins") or {}).get(self._PLUGIN_KEY) or {}
            if key in mine:
                return mine[key]
        except (ImportError, OSError, ValueError, KeyError) as e:
            logger.debug("sqlite config overlay unavailable: %s", e)
        return self._config.get(key, default)

    def get_config_schema(self):
        return [
            {"key": "embedding_url", "description": "OpenAI-compatible embeddings endpoint",
             "required": False, "default": "http://192.168.9.99:8003/v1/embeddings"},
            {"key": "embedding_model", "description": "Embedding model id",
             "required": False, "default": "qwen3-vl-embedding-2b-fp8"},
            {"key": "recall_top_k", "description": "Facts injected per turn",
             "type": "integer", "required": False, "default": 5},
            {"key": "min_score", "description": "Max cosine distance to accept a recall (0..2)",
             "type": "number", "required": False, "default": 0.35},
            {"key": "migrate_from", "description": "Optional legacy graph.sqlite to import once",
             "required": False, "default": ""},
            {"key": "checkpoint_keep", "description": "Pre-compress checkpoints retained (API v2)",
             "type": "integer", "required": False, "default": 20},
        ]

    # -- lifecycle ------------------------------------------------------------

    def initialize(self, session_id: str, **kwargs) -> None:
        self._profile = kwargs.get("agent_identity") or "default"
        self._session_id = session_id
        # hermes_home arrives in kwargs (profile-scoped); fall back to resolver.
        home = kwargs.get("hermes_home")
        path = Path(home) / "memory.sqlite" if home else self._db_path()
        self._open(path)
        self._migrate_if_empty()
        logger.info("sqlite memory ready (%s, profile=%s)", path, self._profile)

    def _open(self, path: Path) -> None:
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(path), check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
            self._conn_path = str(path)

    def _migrate_if_empty(self) -> None:
        src = str(self._cfg("migrate_from", "") or "")
        if not src:
            return
        with self._lock:
            assert self._conn is not None
            if self._conn.execute("SELECT count(*) FROM facts").fetchone()[0]:
                return
            legacy = Path(src).expanduser()
            if not legacy.is_file():
                logger.warning("sqlite memory: migrate_from %s missing", legacy)
                return
            try:
                con = sqlite3.connect(f"file:{legacy}?mode=ro", uri=True)
                # facts (MemFact nodes carry the float-list embedding as JSON string)
                fid_by_id: dict[int, str] = {}
                ins = 0
                for nid, pj in con.execute(
                        "SELECT id, props FROM nodes WHERE labels='MemFact'"):
                    p = json.loads(pj)
                    emb = p.get("embedding")
                    if isinstance(emb, str):
                        try:
                            emb = json.loads(emb)
                        except (ValueError, TypeError) as e:
                            logger.debug("sqlite migration: bad embedding json: %s", e)
                            emb = None
                    embedding = [float(x) for x in emb] if isinstance(emb, list) and emb else None
                    fid = p.get("id") or uuid.uuid4().hex[:12]
                    self._conn.execute(
                        "INSERT OR IGNORE INTO facts(id,text,kind,source_profile,author,"
                        "created_at,valid_at,invalid_at,embedding) VALUES(?,?,?,?,?,?,?,?,?)",
                        (fid, p.get("text", ""), p.get("kind", "fact"),
                         p.get("source_profile", ""), p.get("author", ""),
                         p.get("created_at") or p.get("valid_at") or self._now(),
                         p.get("valid_at") or self._now(), p.get("invalid_at"),
                         _pack(embedding) if embedding else None))
                    ins += 1
                    if nid:
                        fid_by_id[nid] = fid
                # entity links via ABOUT edges (MemFact)-[:ABOUT]->(MemEntity)
                links = 0
                for src_id, dst_id in con.execute(
                        "SELECT e.src, e.dst FROM edges e "
                        "JOIN nodes a ON a.id=e.src AND a.labels='MemFact' "
                        "JOIN nodes b ON b.id=e.dst AND b.labels='MemEntity' "
                        "WHERE e.type='ABOUT'"):
                    ent = json.loads(con.execute(
                        "SELECT props FROM nodes WHERE id=?", (dst_id,)).fetchone()[0])
                    name = str(ent.get("name", "")).strip().lower()
                    fid = fid_by_id.get(src_id)
                    if not name or not fid:
                        continue
                    self._conn.execute("INSERT OR IGNORE INTO entities(name) VALUES(?)", (name,))
                    self._conn.execute("INSERT OR IGNORE INTO fact_entities VALUES(?,?)", (fid, name))
                    links += 1
                con.close()
                self._conn.commit()
                logger.info("sqlite memory: imported %d legacy facts, %d entity links from %s",
                            ins, links, legacy)
            except (sqlite3.Error, OSError, ValueError, KeyError) as e:
                logger.warning("sqlite memory migration failed: %s", e)

    # -- embeddings -------------------------------------------------------------

    def _embed(self, text: str) -> list[float]:
        import httpx
        url = str(self._cfg("embedding_url", "http://192.168.9.100:8003/v1/embeddings"))
        model = str(self._cfg("embedding_model", "qwen3-vl-embedding-2b-fp8"))
        r = httpx.post(url, json={"model": model, "input": text[:4000]}, timeout=30)
        r.raise_for_status()
        return r.json()["data"][0]["embedding"]

    # -- core ops ---------------------------------------------------------------

    def _now(self) -> str:
        return datetime.now(UTC).isoformat()

    # -- pre-compress checkpoint (API v2, fail-closed when required) ----------

    def on_pre_compress(self, messages: list, *, require_checkpoint: bool = False) -> str:
        """Durably snapshot the evidence about to be compressed.

        v2 contract: called with host-normalized direct user/assistant evidence.
        ``require_checkpoint=True`` means the host WILL refuse compression if this
        call raises — so durability errors must propagate, never be swallowed.
        """
        payload = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
        sha = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        try:
            self._write_checkpoint(sha, payload, len(messages))
        except (sqlite3.Error, OSError) as exc:
            if require_checkpoint:
                raise
            logger.warning("sqlite checkpoint skipped (best-effort): %s", exc)
            return ""
        return ""

    def _write_checkpoint(self, sha: str, payload: str, n_messages: int) -> None:
        # Dedicated connection, durability-first: synchronous=FULL makes the commit
        # fsync journal+WAL, so the snapshot survives a crash right after we return.
        assert self._conn_path, "provider not initialized"
        keep = int(self._cfg("checkpoint_keep", 20) or 20)
        conn = sqlite3.connect(self._conn_path, timeout=30)
        try:
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO checkpoints(session_id, profile, ts, sha, n_messages, payload) VALUES (?,?,?,?,?,?)",
                (getattr(self, "_session_id", ""), self._profile, self._now(), sha, n_messages, payload))
            conn.execute(
                "DELETE FROM checkpoints WHERE id NOT IN"
                " (SELECT id FROM checkpoints ORDER BY id DESC LIMIT ?)", (keep,))
            conn.commit()
        finally:
            conn.close()
        self._verify_checkpoint(sha)

    def _verify_checkpoint(self, sha: str) -> None:
        # Open the file fresh: proves the bytes are readable from durable storage,
        # not just sitting in this process's page cache of an open connection.
        conn = sqlite3.connect(f"file:{self._conn_path}?mode=ro", uri=True, timeout=30)
        try:
            row = conn.execute("SELECT payload FROM checkpoints WHERE sha = ?", (sha,)).fetchone()
        finally:
            conn.close()
        if row is None:
            raise sqlite3.DatabaseError(f"checkpoint {sha[:12]} not readable after commit")
        got = hashlib.sha256(row[0].encode("utf-8")).hexdigest()
        if got != sha:
            raise sqlite3.DatabaseError(f"checkpoint {sha[:12]} failed integrity recheck")

    def checkpoint_list(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            assert self._conn is not None
            rows = self._conn.execute(
                "SELECT id, ts, profile, sha, n_messages FROM checkpoints ORDER BY id DESC LIMIT ?",
                (limit,)).fetchall()
        return [{"id": r[0], "ts": r[1], "profile": r[2], "sha": r[3], "n_messages": r[4]}
                for r in rows]

    def checkpoint_payload(self, checkpoint_id: int) -> Optional[list[dict[str, Any]]]:
        with self._lock:
            assert self._conn is not None
            row = self._conn.execute(
                "SELECT payload FROM checkpoints WHERE id = ?", (checkpoint_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def _add_fact(self, text: str, kind: str, entities: list[str],
                  author: str = "", created: Optional[str] = None,
                  invalid_at: Optional[str] = None,
                  embedding: Optional[list[float]] = None) -> str:
        fid = uuid.uuid4().hex[:12]
        if embedding is None:
            try:
                embedding = self._embed(text)
            except (RuntimeError, OSError, KeyError) as e:
                logger.warning("sqlite memory: embed failed, storing without vector: %s", e)
        now = created or self._now()
        with self._lock:
            assert self._conn is not None
            self._conn.execute(
                "INSERT INTO facts(id,text,kind,source_profile,author,created_at,valid_at,invalid_at,embedding)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (fid, text, kind, self._profile, author, now, now, invalid_at,
                 _pack(embedding) if embedding else None))
            for ent in entities[:8]:
                name = ent.strip().lower()
                if not name:
                    continue
                self._conn.execute("INSERT OR IGNORE INTO entities(name) VALUES(?)", (name,))
                self._conn.execute("INSERT OR IGNORE INTO fact_entities VALUES(?,?)", (fid, name))
            self._conn.commit()
        return fid

    def _search(self, query: str, limit: int) -> list[dict]:
        limit = max(1, min(int(limit or 5), 20))
        try:
            vec = self._embed(query)
        except (RuntimeError, OSError, KeyError) as e:
            logger.debug("sqlite memory: query embed failed (%s), FTS fallback", e)
            return self._fts(query, limit)
        with self._lock:
            assert self._conn is not None
            rows = self._conn.execute(
                "SELECT id,text,kind,embedding FROM facts WHERE invalid_at IS NULL").fetchall()
        scored = []
        maxd = float(self._cfg("min_score", 0.35))
        for rid, rtext, rkind, blob in rows:
            if not blob:
                continue
            fv = _unpack(blob)
            # cosine distance
            dot = sum(a * b for a, b in zip(vec, fv))
            na = sum(a * a for a in vec) ** 0.5
            nb = sum(b * b for b in fv) ** 0.5
            if na == 0 or nb == 0:
                continue
            d = 1.0 - dot / (na * nb)
            if d < maxd:
                scored.append({"id": rid, "text": rtext, "kind": rkind, "score": round(d, 4)})
        scored.sort(key=lambda r: r["score"])
        if not scored:
            fts = self._fts(query, limit)
            return fts
        return scored[:limit]

    def _fts(self, query: str, limit: int) -> list[dict]:
        from sqlite3 import OperationalError
        with self._lock:
            assert self._conn is not None
            try:
                rows = self._conn.execute(
                    "SELECT f.id,f.text,f.kind FROM facts_fts j "
                    "JOIN facts f ON f.rowid = facts_fts.rowid "
                    "WHERE facts_fts MATCH ? AND f.invalid_at IS NULL LIMIT ?",
                    (query, max(1, min(int(limit or 5), 20)))).fetchall()
            except OperationalError:
                return []
        return [{"id": r[0], "text": r[1], "kind": r[2], "score": 0.0} for r in rows]

    def _related(self, entity: str, limit: int) -> list[dict]:
        with self._lock:
            assert self._conn is not None
            rows = self._conn.execute(
                "SELECT f.id,f.text,f.kind FROM facts f "
                "JOIN fact_entities fe ON fe.fact_id=f.id "
                "WHERE fe.name=? AND f.invalid_at IS NULL "
                "ORDER BY f.created_at DESC LIMIT ?",
                (entity.strip().lower(), max(1, min(int(limit or 10), 30)))).fetchall()
        return [{"id": r[0], "text": r[1], "kind": r[2], "score": 0.0} for r in rows]

    def _invalidate(self, fact_id: str) -> bool:
        with self._lock:
            assert self._conn is not None
            cur = self._conn.execute(
                "UPDATE facts SET invalid_at=? WHERE id=? AND invalid_at IS NULL",
                (self._now(), fact_id))
            self._conn.commit()
        return cur.rowcount > 0

    def _stats(self) -> dict:
        with self._lock:
            assert self._conn is not None
            total = self._conn.execute("SELECT count(*) FROM facts").fetchone()[0]
            live = self._conn.execute("SELECT count(*) FROM facts WHERE invalid_at IS NULL").fetchone()[0]
            ents = self._conn.execute("SELECT count(*) FROM entities").fetchone()[0]
            novec = self._conn.execute("SELECT count(*) FROM facts WHERE embedding IS NULL").fetchone()[0]
        return {"facts_total": total, "facts_valid": live, "entities": ents,
                "facts_without_vector": novec, "db": self._conn_path}

    # -- recall hooks -------------------------------------------------------------

    def system_prompt_block(self) -> str:
        return (
            "Local long-term memory is active (SQLite + vector recall, "
            "tool sqlite_memory). Relevant facts may be auto-injected below. "
            "Store durable knowledge there with sqlite_memory (action=add); "
            "keep only always-on personal context in the built-in memory tool."
        )

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        if not query or self._conn is None:
            return
        spawn_context_thread(self._prefetch_worker, name="sqlite-mem-prefetch",
                             args=(query,)).start()

    def _prefetch_worker(self, query: str) -> None:
        try:
            rows = self._search(query, int(self._cfg("recall_top_k", 5)))
            if rows:
                lines = "\n".join(f"- ({r['kind']}) {r['text']}" for r in rows)
                self._prefetch_cache[query] = "## Recalled from local memory\n" + lines
                self._last_recall_count = len(rows)
        except (RuntimeError, OSError, KeyError, sqlite3.Error) as e:
            logger.debug("sqlite prefetch failed: %s", e)

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not self._prefetch_cache:
            return ""
        keys = list(self._prefetch_cache)
        return self._prefetch_cache.pop(keys[-1], "")

    def recall_status(self):
        from agent.memory_provider import RecallStatus
        if not self._last_recall_count:
            return None
        return RecallStatus(provider_label="sqlite", count=self._last_recall_count, glyph="🧠")

    def on_memory_write(self, action: str, target: str, content: str,
                        metadata: Optional[dict[str, Any]] = None) -> None:
        if action != "add" or self._conn is None:
            return
        try:
            self._add_fact(content, kind="preference" if target == "user" else "fact",
                           entities=[])
        except (RuntimeError, OSError, KeyError, sqlite3.Error) as e:
            logger.debug("sqlite memory mirror failed: %s", e)

    # -- tools ---------------------------------------------------------------------

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return [MEMORY_TOOL_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs) -> str:
        try:
            if tool_name != "sqlite_memory":
                return json.dumps({"error": f"unknown tool {tool_name}"})
            action = args.get("action")
            if action == "add":
                text = (args.get("text") or "").strip()
                if not text:
                    return json.dumps({"error": "text required"})
                fid = self._add_fact(text, args.get("kind", "fact"),
                                     args.get("entities") or [])
                return json.dumps({"ok": True, "id": fid})
            if action == "search":
                q = (args.get("text") or "").strip()
                if not q:
                    return json.dumps({"error": "text required"})
                return json.dumps({"results": self._search(q, args.get("limit", 5))})
            if action == "related":
                e = (args.get("entity") or "").strip()
                if not e:
                    return json.dumps({"error": "entity required"})
                return json.dumps({"results": self._related(e, args.get("limit", 10))})
            if action == "invalidate":
                return json.dumps({"ok": self._invalidate(args.get("fact_id") or "")})
            if action == "stats":
                return json.dumps(self._stats())
            return json.dumps({"error": f"unknown action {action}"})
        except Exception as e:
            logger.exception("sqlite memory tool error")
            return json.dumps({"error": str(e)[:300]})

    def backup_paths(self) -> list[str]:
        try:
            return [str(self._db_path())]
        except (sqlite3.Error, OSError) as e:
            logger.debug("sqlite backup_paths unavailable: %s", e)
            return []

    def shutdown(self) -> None:
        with self._lock:
            if self._conn:
                try:
                    self._conn.commit()
                    self._conn.close()
                except sqlite3.Error as e:
                    logger.debug("sqlite shutdown: %s", e)
            self._conn = None


def _load_plugin_config() -> dict:
    try:
        from hermes_cli.config import load_config_readonly, cfg_get
        return cfg_get(load_config_readonly(), "plugins", "sqlite", default={}) or {}
    except (ImportError, OSError, ValueError, KeyError) as e:
        logger.debug("sqlite plugin config unavailable: %s", e)
        return {}


def register(ctx) -> None:
    ctx.register_memory_provider(
        SqliteMemoryProvider(config=_load_plugin_config())
    )

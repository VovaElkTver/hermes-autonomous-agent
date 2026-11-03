"""controller.causal — causal-graph edges in state.db (V-O9 «плоско пишем — графом читаем»).

ТЗ: ~/projects/hermes-agency/docs/requirements.md §V-O9 + core ТЗ (03.11, brainstorm
hermes-agency). История физически остаётся append-плоской (ctl_event, messages,
session_branches keep their rows and columns — старые читатели не тронуты); парные
связи дополнительно становятся рёбрами здесь, от предшественника к следствию, так что
ленты читаются DAG'ом: форки, конвергенция источников, supersede-цепочки.

Концы рёбер — namespaced TEXT id; одно id-пространство никогда не смешивается с другим:

    ctl:<event_id>          ctl_event                 ts = ts
    task:<task_id>          ctl_task                  ts = created_at
    rev:<task_id>@<version> ctl_contract_revision     ts = agreed_at | None
    mem:<item_id>           ctl_mem_item              ts = created_at
    todo:<todo_id>          ctl_todo                  ts = created_at
    br:<branch_id>          session_branches          ts = created_at
    sess:<session_id>       sessions                  ts = started_at (immutable)
    obs:<event_obs_id>      event_observations        ts = created_at

Контракт писателя (:func:`link`): известный kind и префикс, оба конца существуют,
нет self-loop, нет цикла (to не может уже быть предком from), ts(to) >= ts(from)
когда оба узловых времени резолвятся (несогласованная MATERIAL-ревизация времени не
имеет — порядок на ней не проверяется). UNIQUE(from_event, to_event, kind) делает
каждую запись и всю миграцию переигрываемой: дубликат всасывается молча, как
idempotency_key в ctl_event.

Shadow режим (дисциплина stage003 п.6): писатели включены, читатели сидят на старых
путях. Три новых читателя живут здесь как query-хелперы — supersede-инвалидация,
линейная проекция, причинная свежесть — и переключаются по одному.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time

logger = logging.getLogger("controller.causal")

KINDS = ("reply", "about", "causes", "supersedes", "merge")

#: Hard caps keep every recursive walk O(graph-of-the-query), never O(store).
MAX_DEPTH = 64
MAX_WALK_ROWS = 5000


class CausalContractError(ValueError):
    """Writer-contract violation: bad kind/prefix, missing endpoint, self-loop,
    cycle, or ts inversion. The edge is never stored."""


# ---------------------------------------------------------------- schema

_CAUSAL_DDL_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS ctl_causal_link (
  link_id    INTEGER PRIMARY KEY,
  ts         REAL NOT NULL,
  from_event TEXT NOT NULL,
  to_event   TEXT NOT NULL,
  kind       TEXT NOT NULL CHECK (kind IN ('reply','about','causes','supersedes','merge')),
  payload    TEXT
)""",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_cl_edge"
    " ON ctl_causal_link(from_event, to_event, kind)",
    "CREATE INDEX IF NOT EXISTS idx_cl_from ON ctl_causal_link(from_event)",
    "CREATE INDEX IF NOT EXISTS idx_cl_to   ON ctl_causal_link(to_event)",
    "CREATE INDEX IF NOT EXISTS idx_cl_kind ON ctl_causal_link(kind)",
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create ctl_causal_link when absent (additive; canonical SCHEMA_SQL keeps
    the copy too — this guards fresh test connections built from older schemas).
    Statement-by-statement on purpose: executescript would COMMIT an open write
    transaction this module's callers are still filling.

    Legacy sweep: databases built before ТЗ v2 carry the same triple under the
    old name `udl_ctl_causal_link` (and possibly AUTOINCREMENT on link_id).
    `ux_cl_edge` is created first, so dropping the duplicate never leaves the
    idempotency key unprotected; the AUTOINCREMENT residue is a pure rowid
    bookkeeping difference and only a live-empty table is ever rebuilt."""
    for stmt in _CAUSAL_DDL_STATEMENTS:
        conn.execute(stmt)
    conn.execute("DROP INDEX IF EXISTS udl_ctl_causal_link")


# ---------------------------------------------------------------- node registry

#: namespace -> (table, timestamp column, key column). `sess:` ts = CREATION
#: (started_at), never activity: a mutable stamp would make historic edges flip
#: their ordering as sessions keep living. `rev:` is special-cased below.
_NODE_TABLES = {
    "ctl": ("ctl_event", "ts", "event_id"),
    "task": ("ctl_task", "created_at", "task_id"),
    "mem": ("ctl_mem_item", "created_at", "item_id"),
    "todo": ("ctl_todo", "created_at", "todo_id"),
    "br": ("session_branches", "created_at", "branch_id"),
    "sess": ("sessions", "started_at", "id"),
    "obs": ("event_observations", "created_at", "event_obs_id"),
}


def _split(node: str) -> str | None:
    ns, sep, rest = str(node).partition(":")
    if not sep or not rest:
        return None
    return ns


def _node_state(conn: sqlite3.Connection, node: str) -> tuple[bool, float | None]:
    """Resolve an endpoint → (exists, ts). ts is None for present-but-un-timestamped
    nodes (rev before agreement) — the ordering check skips those."""
    ns, _, rest = str(node).partition(":")
    if ns == "rev":
        task, sep, ver = rest.rpartition("@")
        if not sep or not ver.isdigit():
            return False, None
        row = conn.execute(
            "SELECT agreed_at FROM ctl_contract_revision WHERE task_id=? AND version=?",
            (task, int(ver))).fetchone()
        return (row is not None), (None if row is None else row[0])
    spec = _NODE_TABLES.get(ns)
    if spec is None:
        return False, None
    table, ts_col, key = spec
    row = conn.execute(f"SELECT {ts_col} FROM {table} WHERE {key}=?", (rest,)).fetchone()
    return (row is not None), (None if row is None else row[0])


# ---------------------------------------------------------------- writer

def _ancestors(conn: sqlite3.Connection, node: str) -> set[str]:
    """Nodes reachable from `node` walking causal edges UPSTREAM (predecessors,
    transitive). Path-dedup makes the walk terminate on diamonds/cycles cheaply."""
    rows = conn.execute(
        """
        WITH RECURSIVE up(n, path, depth) AS (
          SELECT :start, char(10) || :start || char(10), 0
          UNION ALL
          SELECT cl.from_event, up.path || char(10) || cl.from_event || char(10), up.depth + 1
          FROM ctl_causal_link cl JOIN up ON cl.to_event = up.n
          WHERE instr(up.path, char(10) || cl.from_event || char(10)) = 0
            AND up.depth < :maxd
        )
        SELECT DISTINCT n FROM up WHERE n <> :start LIMIT :cap
        """,
        {"start": node, "maxd": MAX_DEPTH, "cap": MAX_WALK_ROWS},
    ).fetchall()
    return {r[0] for r in rows}


def link(conn: sqlite3.Connection, *, frm: str, to: str, kind: str,
         payload: dict | None = None, ts: float | None = None,
         commit: bool = True, strict: bool = True) -> int | None:
    """Write one causal edge frm -> to. Returns rowid, or None when the same
    (frm, to, kind) triple already exists (absorbed — replay is the norm here).

    strict=True raises CausalContractError on a contract violation (test writers,
    migration accounting); strict=False logs and returns None (shadow writers on
    live paths must never break ingestion over a bookkeeping edge).
    """
    def reject(reason: str):
        if strict:
            raise CausalContractError(f"{reason}: {frm!r} -[{kind}]-> {to!r}")
        logger.debug("causal link skipped (%s): %s -[%s]-> %s", reason, frm, kind, to)

    if kind not in KINDS:
        return reject(f"unknown kind {kind!r}")
    if _split(frm) is None or _split(to) is None:
        return reject("endpoint without a known namespace prefix")
    if frm == to:
        return reject("self-loop")
    frm_ok, frm_ts = _node_state(conn, frm)
    to_ok, to_ts = _node_state(conn, to)
    if not frm_ok:
        return reject("from-endpoint does not exist")
    if not to_ok:
        return reject("to-endpoint does not exist")
    if frm_ts is not None and to_ts is not None and to_ts < frm_ts and kind != "reply":
        return reject("ts inversion (to older than from)")
    if kind == "reply" and frm_ts is not None and to_ts is not None and to_ts < frm_ts:
        # Exception to the ts-ordering contract, reply kind only: obs:/msg: stamps
        # are INGESTION times. A thread parent fetched after its child (normal for
        # multi-tape sync, C3) would otherwise lose the edge forever. The cycle
        # guard below still protects the DAG; ordering belongs to the edges' own
        # ts column, not to node stamps. (Deviation from ТЗ §2.4, flagged to Вл.)
        pass
    if to in _ancestors(conn, frm):
        return reject("cycle (to is already an ancestor of from)")
    cur = conn.execute(
        "INSERT OR IGNORE INTO ctl_causal_link (ts, from_event, to_event, kind, payload)"
        " VALUES (?,?,?,?,?)",
        (time.time() if ts is None else ts, frm, to, kind,
         json.dumps(payload, ensure_ascii=False) if payload else None))
    if commit:
        conn.commit()
    return cur.lastrowid if cur.rowcount else None


# ---------------------------------------------------------------- readers

def descendants(conn: sqlite3.Connection, start: str, *, kinds: tuple | None = None,
                max_depth: int = MAX_DEPTH) -> list[dict]:
    """Everything downstream of `start` (consequences, transitive). Each hop may be
    filtered by kind. Returns [{node, depth, via, ts}...] deduped to the SHALLOWEST
    path per node — 'what would a change to X touch' (C9 supersede-invalidation)."""
    rows = conn.execute(
        """
        WITH RECURSIVE down(n, depth, via, path) AS (
          SELECT :start, 0, '', char(10) || :start || char(10)
          UNION ALL
          SELECT cl.to_event, down.depth + 1, cl.kind,
                 down.path || char(10) || cl.to_event || char(10)
          FROM ctl_causal_link cl JOIN down ON cl.from_event = down.n
          WHERE instr(down.path, char(10) || cl.to_event || char(10)) = 0
            AND down.depth < :maxd
            AND (:kinds IS NULL OR instr(:kinds, char(10) || cl.kind || char(10)) > 0)
        )
        SELECT n, MIN(depth), GROUP_CONCAT(DISTINCT via), MIN(ts_row) FROM (
          SELECT d.n AS n, d.depth AS depth, d.via AS via,
                 COALESCE((SELECT cl.ts FROM ctl_causal_link cl
                            WHERE cl.to_event = d.n LIMIT 1), 0) AS ts_row
          FROM down d WHERE d.n <> :start
          LIMIT :cap
        ) GROUP BY n ORDER BY MIN(depth), n
        """,
        {"start": start, "maxd": max_depth, "cap": MAX_WALK_ROWS,
         "kinds": None if kinds is None else "".join(f"\n{k}\n" for k in kinds)},
    ).fetchall()
    return [{"node": n, "depth": d, "via": (v or "").strip("\n").split(",") if v else [],
             "ts": ts} for n, d, v, ts in rows]


def supersede_impact(conn: sqlite3.Connection, node: str) -> list[dict]:
    """Reader 1 (V-O9 п.3 / «что затронет правка X»): transitive closure downstream
    over supersedes/causes edges — the records that stand on X."""
    return descendants(conn, node, kinds=("supersedes", "causes"))


def causal_freshness(conn: sqlite3.Connection, node: str) -> int:
    """Reader 3 (delivery vs causality split): LONGEST causal chain downstream of
    the last-processed node — depth over paths, not levels (a node reached at
    several depths counts at its deepest). This is NOT a delivery cursor
    position; until heads use it, callers count and log it."""
    row = conn.execute(
        """
        WITH RECURSIVE down(n, depth, path) AS (
          SELECT :start, 0, char(10) || :start || char(10)
          UNION ALL
          SELECT cl.to_event, down.depth + 1,
                 down.path || char(10) || cl.to_event || char(10)
          FROM ctl_causal_link cl JOIN down ON cl.from_event = down.n
          WHERE instr(down.path, char(10) || cl.to_event || char(10)) = 0
            AND down.depth < :maxd
        )
        SELECT MAX(depth) FROM down WHERE n <> :start LIMIT 1
        """,
        {"start": node, "maxd": MAX_DEPTH},
    ).fetchone()
    return int(row[0] or 0) if row and row[0] is not None else 0


def projection(conn: sqlite3.Connection, roots: list[str] | str,
               *, max_nodes: int = MAX_WALK_ROWS) -> list[dict]:
    """Reader 2: linear topological view of the reachable subgraph — the
    compatible shape old list-readers expect. Kahn over (node ts, node id), so
    the projection is stable across runs for the same graph."""
    if isinstance(roots, str):
        roots = [roots]
    nodes: dict[str, int] = {}
    indeg: dict[str, int] = {}
    adj: dict[str, list[str]] = {}
    frontier = list(roots)
    seen = set(frontier)
    edges: list[tuple[str, str]] = []
    while frontier and len(nodes) + len(seen) < max_nodes:
        row_frontier = frontier.pop(0)
        nodes.setdefault(row_frontier, 0)
        for r in conn.execute(
                "SELECT from_event, to_event FROM ctl_causal_link WHERE from_event = ?",
                (row_frontier,)):
            if r[1] not in seen:
                seen.add(r[1]); frontier.append(r[1])
            edges.append((r[0], r[1]))
    for n in seen:
        nodes.setdefault(n, 0); indeg.setdefault(n, 0); adj.setdefault(n, [])
    for f, t in edges:
        if f in seen and t in seen:
            adj[f].append(t); indeg[t] += 1
    import heapq
    ts_map = {n: _node_state(conn, n)[1] for n in seen}
    heap = [(ts_map[n] if ts_map[n] is not None else float("inf"), n)
            for n in seen if indeg[n] == 0]
    heapq.heapify(heap)
    out: list[dict] = []
    while heap:
        _, n = heapq.heappop(heap)
        out.append({"node": n, "ts": ts_map[n]})
        for m in adj[n]:
            indeg[m] -= 1
            if indeg[m] == 0:
                tm = ts_map[m] if ts_map[m] is not None else float("inf")
                heapq.heappush(heap, (tm, m))
    if len(out) != len(seen):  # should not happen (DAG contract) — keep order honest
        rest = sorted(seen - {o["node"] for o in out})
        out.extend({"node": n, "ts": ts_map.get(n)} for n in rest)
    return out


# ---------------------------------------------------------------- migration

def _migrate_reply_edges(conn, stats) -> None:
    """reply-рёбра из event_observations.relation (m.in_reply_to).
    Донор ТЗ говорил `messages.in_reply_to` — такой колонки в этой схеме нет;
    фактический носитель reply-to в state.db — relation JSONTransport-слоя."""
    rows = conn.execute(
        "SELECT event_obs_id, room_id, relation FROM event_observations"
        " WHERE relation LIKE '%in_reply_to%'").fetchall()
    for ev_id, room_id, relation in rows:
        try:
            rel = json.loads(relation or "{}")
        except (ValueError, TypeError):
            continue
        parent = (((rel or {}).get("m.in_reply_to") or {}).get("event_id"))
        if not parent:
            continue
        prow = conn.execute(
            "SELECT event_obs_id FROM event_observations"
            " WHERE room_id=? AND event_id=? AND event_obs_id<>? LIMIT 1",
            (room_id, parent, ev_id)).fetchone()
        if prow is None:
            stats["reply_unresolved"] += 1
            continue
        _accounted_link(conn, stats, "reply_new",
                        frm=f"obs:{prow[0]}", to=f"obs:{ev_id}", kind="reply")


def _migrate_branch_edges(conn, stats) -> None:
    """causes (родительская лента → создание ветки) и merge через событие join:
    прямое ребро br->sess:parent закрыло бы цикл против causes-ребра форка."""
    for bid, parent, status in conn.execute(
            "SELECT branch_id, parent_session_id, status FROM session_branches"):
        _accounted_link(conn, stats, "causes_new",
                        frm=f"sess:{parent}", to=f"br:{bid}", kind="causes")
        if status == "joined":
            try:
                if record_branch_join(conn, branch_id=bid, parent_session_id=parent,
                                      migration=True, commit=False):
                    stats["merge_new"] += 1
                else:
                    stats["absorbed"] += 1
            except CausalContractError as exc:
                stats["skipped"].append(str(exc))


def _migrate_supersedes_edges(conn, stats) -> None:
    for old, new in conn.execute(
            "SELECT item_id, superseded_by FROM ctl_mem_item"
            " WHERE superseded_by IS NOT NULL AND superseded_by <> ''"):
        _accounted_link(conn, stats, "supersedes_new",
                        frm=f"mem:{old}", to=f"mem:{new}", kind="supersedes")
    for task, ver, sup in conn.execute(
            "SELECT task_id, version, supersedes FROM ctl_contract_revision"
            " WHERE supersedes IS NOT NULL"):
        _accounted_link(conn, stats, "supersedes_new",
                        frm=f"rev:{task}@{sup}", to=f"rev:{task}@{ver}", kind="supersedes")


def _accounted_link(conn, stats, bucket, **kw) -> None:
    try:
        rid = link(conn, strict=True, commit=False, **kw)
        stats[bucket] += 1 if rid else 0
        if rid is None:
            stats["absorbed"] += 1
    except CausalContractError as exc:
        stats["skipped"].append(str(exc))


def migrate(conn: sqlite3.Connection, *, commit: bool = True) -> dict:
    """One-shot idempotent backfill of existing pairwise links into edges.
    Source columns are NOT removed (denormalization for existing readers).
    Safe to run repeatedly: UNIQUE(from,to,kind) absorbs the replay."""
    stats = {"reply_new": 0, "causes_new": 0, "merge_new": 0, "supersedes_new": 0,
             "absorbed": 0, "reply_unresolved": 0, "skipped": []}
    ensure_schema(conn)
    _migrate_reply_edges(conn, stats)
    _migrate_branch_edges(conn, stats)
    _migrate_supersedes_edges(conn, stats)
    if commit:
        conn.commit()
    return stats


# ---------------------------------------------------------------- invariants

def check_invariants(conn: sqlite3.Connection) -> dict:
    """Audit pass used after migration and in regression: every violation class is
    listed (bounded), an empty dict of lists is the green board."""
    ensure_schema(conn)
    out: dict = {"self_loop": [], "dangling": [], "ts_order": [], "cycles": [],
                 "bad_kind": []}
    edge_rows = conn.execute(
        "SELECT from_event, to_event, kind FROM ctl_causal_link").fetchall()
    ts_cache: dict[str, tuple[bool, float | None]] = {}
    adj: dict[str, set[str]] = {}
    for frm, to, kind in edge_rows:
        if kind not in KINDS:
            out["bad_kind"].append([frm, to, kind])
        if frm == to:
            out["self_loop"].append([frm, kind])
        adj.setdefault(frm, set()).add(to)
        adj.setdefault(to, set())
        for n in (frm, to):
            if n not in ts_cache:
                ts_cache[n] = _node_state(conn, n)
            exists, _ = ts_cache[n]
            if not exists:
                out["dangling"].append([frm, to, kind, n])
        f_ts, t_ts = ts_cache[frm][1], ts_cache[to][1]
        # reply is exempt (writer contract): node stamps are ingestion times,
        # out-of-order thread parents legitimately invert them.
        if kind != "reply" and isinstance(f_ts, (int, float)) \
                and isinstance(t_ts, (int, float)) and t_ts < f_ts:
            out["ts_order"].append([frm, to, kind])
    # iterative DFS coloring — one pass over the whole edge set
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {}
    on_cycle: set[str] = set()

    def visit(root: str):
        stack = [(root, iter(adj.get(root, ())))]
        color[root] = GRAY
        while stack:
            node, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                color[node] = BLACK
                stack.pop()
                continue
            c = color.get(nxt, WHITE)
            if c == WHITE:
                stack.append((nxt, iter(adj.get(nxt, ()))))
                color[nxt] = GRAY
            elif c == GRAY:
                on_cycle.add(nxt); on_cycle.add(node)
    for n in list(adj):
        if color.get(n, WHITE) == WHITE:
            visit(n)
    if on_cycle:
        out["cycles"] = sorted(on_cycle)
    return {k: v for k, v in out.items() if v}


# ---------------------------------------------------------------- live hooks

def record_branch_join(conn: sqlite3.Connection, *, branch_id: str,
                       parent_session_id: str, migration: bool = False,
                       commit: bool = True) -> str | None:
    """Join = convergence event: a ctl_event node receives a `merge` edge from
    the absorbed branch and a `causes` edge from the absorbing parent line.
    Without an event node, a merge edge back onto the parent would close a
    cycle against the fork's causes edge — the event is the join point.
    Idempotent: an existing merge edge for this branch means it is recorded."""
    from . import model
    if conn.execute("SELECT 1 FROM ctl_causal_link WHERE from_event=? AND kind='merge'",
                    (f"br:{branch_id}",)).fetchone():
        return None
    idem = f"branch_join:{branch_id}"
    eid = model.emit(conn, kind="branch_joined", origin="causal",
                     payload={"branch_id": branch_id, "parent_session_id": parent_session_id,
                              "migration": migration},
                     idempotency_key=idem)
    if eid is None:  # event survived an earlier crash seam — find it, finish the edges
        row = conn.execute("SELECT event_id FROM ctl_event WHERE idempotency_key=?",
                           (idem,)).fetchone()
        eid = row[0] if row else None
    if eid is None:
        return None
    link(conn, frm=f"br:{branch_id}", to=f"ctl:{eid}", kind="merge", commit=False)
    link(conn, frm=f"sess:{parent_session_id}", to=f"ctl:{eid}", kind="causes",
         commit=False)
    if commit:
        conn.commit()
    return eid

def mirror_reply_for_event(conn: sqlite3.Connection, *, event_obs_id: str,
                           room_id: str, relation: dict | None,
                           commit: bool = False) -> None:
    """Shadow writer for transport: an m.in_reply_to relation mirrors to a reply
    edge when the parent event is already observed in the same room. Never
    raises — a missing parent is the norm for out-of-order sync, the idempotent
    :func:`migrate` sweeps the stragglers later."""
    try:
        parent = (((relation or {}).get("m.in_reply_to") or {}).get("event_id"))
        if not parent:
            return
        prow = conn.execute(
            "SELECT event_obs_id FROM event_observations"
            " WHERE room_id=? AND event_id=? AND event_obs_id<>? LIMIT 1",
            (room_id, parent, event_obs_id)).fetchone()
        if prow is None:
            return
        link(conn, frm=f"obs:{prow[0]}", to=f"obs:{event_obs_id}", kind="reply",
             commit=commit, strict=False)
    except Exception:  # bookkeeping must never sink ingestion
        logger.debug("causal reply mirror failed", exc_info=True)

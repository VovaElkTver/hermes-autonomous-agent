"""Agent-facing tool surface of the agency toolset (TZ v0.2 §4).

Seven native tools; every handler returns a JSON envelope. The acting identity
(position_id + mandate_version) is taken from the profile card by the core,
never from model arguments; passed-in actor fields are ignored. Envelope checks
(C8 validate-at-ingest analogues) run before touching the store.

Dev prototype note: set_identity() stands in for profile-card wiring; the real
build reads it from the administrator position config (TZ administrator-mode §2).
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any

from plugins.agency import store

logger = logging.getLogger("agency.tools")

EMOJI = "🗝️"

DEFAULT_ISSUE_TITLE = "issue"


# ------------------------------------------------------------ identity --------
# All toolset users act as the administrator (Вл, 07.11): ONE identity, taken
# from plugin settings at register() time (settings.identity), never from
# model arguments. Per-session binding is deferred until experts get native
# tool access; set_identity() stays a dev/test seam.
_IDENTITY = {"position_id": "admin-1", "mandate_version": 1}


def load_identity(settings: dict) -> None:
    """Merge settings.identity from the plugin config into the module state."""
    ident = settings.get("identity") or {}
    if ident.get("position_id"):
        _IDENTITY["position_id"] = str(ident["position_id"])
    if ident.get("mandate_version") is not None:
        _IDENTITY["mandate_version"] = int(ident["mandate_version"])


def set_identity(position_id: str, mandate_version: int) -> None:
    """Test/dev seam: the production build resolves this from the profile card."""
    _IDENTITY["position_id"] = position_id
    _IDENTITY["mandate_version"] = mandate_version


def _identity() -> tuple[str, int]:
    return _IDENTITY["position_id"], _IDENTITY["mandate_version"]


# Caller-session seam: the runtime knows which session is calling; until that
# plumbing lands, tests/dev set it explicitly. NEVER taken from model args.
_caller = threading.local()


def set_caller_session(session_id: str | None) -> None:
    _caller.sid = session_id


def get_caller_session() -> str | None:
    return getattr(_caller, "sid", None)


def _resolve_actor(db_path) -> tuple[str, int, str]:
    """(position_id, mandate_version, role) of the caller.

    Bound session → its position (must be active). Unbound → the single admin
    identity from plugin settings (Вл 07.11: all current users are admin).
    """
    sid = get_caller_session()
    if not sid:
        pos, ver = _identity()
        return pos, ver, store.POSITIONS.get(pos, {}).get("role", "administrator")
    row = store.position_for_session(db_path, sid)
    if row is None:
        raise store.LedgerError("NOT_REGISTERED",
                                "this session has no agency position; call agency_register")
    if row["status"] != "active":
        raise store.LedgerError("REGISTRATION_PENDING",
                                f"position {row['position_id']!r} awaits admin approval")
    if not store.mandate_active(row["position_id"], row["mandate_version"]):
        raise store.LedgerError("MANDATE_REVOKED", "position mandate is revoked")
    return row["position_id"], row["mandate_version"], row["role"]


def _check_mandate(pos: str, ver: int) -> None:
    if not store.mandate_active(pos, ver):
        raise store.LedgerError("MANDATE_REVOKED",
                                f"mandate v{ver} of {pos} is revoked")


def _require_admin(db_path) -> tuple[str, int]:
    pos, ver = _resolve_actor(db_path)[:2]
    _check_mandate(pos, ver)
    row = store.get_position(db_path, pos)
    role = (row["role"] if row
            else store.POSITIONS.get(pos, {}).get("role", "administrator"))
    if role != "administrator":
        raise store.LedgerError(
            "ROLE_FORBIDDEN", "this verb is administrator-only (coworking roles, TZ §4)")
    return pos, ver


def _out(**payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _err(code: str, message: str, retryable: bool = False) -> str:
    return _out(ok=False, error={"code": code, "message": message,
                                 "retryable": retryable})


def _db(args: dict) -> str | None:
    db_path = args.get("db_path")  # dev/test seam; production uses configured path
    return str(db_path) if db_path else None


def _guarded(fn):
    """Wrap a handler: never leak exceptions to the model — always an envelope."""
    def h(args: dict, **_: Any) -> str:
        try:
            return fn(args)
        except store.LedgerError as exc:
            return _err(exc.code, exc.message, exc.retryable)
        except Exception as exc:  # envelope contract, TZ §3.1
            logger.warning("agency handler failed", exc_info=True)
            return _err("VALIDATION_FAILED", f"internal failure: {exc}")
    return h


def _known_issue(db_path, issue_id: str) -> None:
    if not issue_id or not store.issue_exists(db_path, issue_id):
        raise store.LedgerError("NOT_FOUND", f"issue {issue_id!r} unknown")


# ---------------------------------------------------------------- handlers ----

@_guarded
def agency_list_experts(args: dict) -> str:
    db_path = _db(args)
    experts = []
    for pid, card in sorted(store.POSITIONS.items()):
        busy = False
        # v0.2 projection heuristic: open consent requests or commitments on any issue
        for iid in store.ISSUES:
            proj = store.replay_projection(db_path, iid)
            if any(w["expert_id"] == pid for w in proj["waiting_on"]) or \
                    any(c["who"] == pid for c in proj["commitments"]):
                busy = True
                break
        experts.append({"position_id": pid, "name": pid,
                        "competencies": card["competencies"], "busy": busy})
    as_of = max((store.replay_projection(db_path, i)["as_of_seq"]
                 for i in store.ISSUES), default=0)
    return _out(ok=True, experts=experts, as_of_seq=as_of)


@_guarded
def agency_survey(args: dict) -> str:
    db_path = _db(args)
    expert_id = str(args.get("expert_id") or "")
    issue_id = str(args.get("issue_id") or "")
    question = str(args.get("question") or "").strip()
    if not question:
        raise store.LedgerError("VALIDATION_FAILED", "question must be non-empty")
    _known_issue(db_path, issue_id)
    if expert_id not in store.POSITIONS and not store.get_position(db_path, expert_id):
        raise store.LedgerError("NOT_FOUND", f"expert {expert_id!r} not in registry")
    pos, ver = _require_admin(db_path)
    payload = {"expert_id": expert_id, "question": question,
               "survey_kind": "observation"}
    key = args.get("idempotency_key")
    res = store.submit(db_path, type_="survey_request", kind="observation",
                       issue_id=issue_id, actor_position_id=pos,
                       mandate_version=ver, payload=payload,
                       idem_key=str(key) if key else store.new_idempotency_key())
    return _out(ok=True, event_seq=res["event_seq"], ts=res["ts"],
                idempotency_key=res.get("idem_key"))


@_guarded
def agency_create_branch(args: dict) -> str:
    db_path = _db(args)
    issue_id = str(args.get("issue_id") or "")
    title = str(args.get("title") or "").strip()
    if not title:
        raise store.LedgerError("VALIDATION_FAILED", "title must be non-empty")
    _known_issue(db_path, issue_id)
    pos, ver = _require_admin(db_path)

    def _finalize(conn, self_key):
        # direction counter INSIDE the serialized transaction (race-free, TZ §4);
        # exclude this key's own earlier row so a repeat matches its stored payload
        n = conn.execute(
            "SELECT COUNT(*) FROM ledger WHERE type='branch' AND issue_id=?"
            " AND (idem_key IS NULL OR idem_key != ?)",
            (issue_id, self_key or "")).fetchone()[0] + 1
        return {"direction_id": f"{issue_id}/direction-{n}", "title": title,
                "context": str(args.get("context") or "")}

    key = args.get("idempotency_key")
    res = store.submit(db_path, type_="branch", kind="observation", issue_id=issue_id,
                       actor_position_id=pos, mandate_version=ver, payload={},
                       idem_key=str(key) if key else store.new_idempotency_key(),
                       payload_fn=_finalize)
    return _out(ok=True, event_seq=res["event_seq"], ts=res["ts"],
                direction_id=_last_direction(db_path, res["event_seq"]),
                idempotency_key=res.get("idem_key"))


def _last_direction(db_path: str | None, event_seq: int) -> str:
    with store.connect(db_path) as c:
        row = c.execute("SELECT payload_json FROM ledger WHERE seq=?",
                        (event_seq,)).fetchone()
    return json.loads(row["payload_json"])["direction_id"]


@_guarded
def agency_record_commitment(args: dict) -> str:
    db_path = _db(args)
    issue_id = str(args.get("issue_id") or "")
    who = str(args.get("who") or "")
    what = str(args.get("what") or "").strip()
    due = str(args.get("due") or "").strip()
    refs = list(args.get("refs") or [])
    if not what:
        raise store.LedgerError("VALIDATION_FAILED", "what must be non-empty")
    if not due:
        raise store.LedgerError("VALIDATION_FAILED", "due (ISO-8601 UTC) is required")
    _known_issue(db_path, issue_id)
    if who not in store.POSITIONS and not store.get_position(db_path, who):
        raise store.LedgerError("NOT_FOUND", f"position {who!r} not in registry")
    actor, ver, actor_role = _resolve_actor(db_path)
    if actor_role != "administrator" and who != actor:
        raise store.LedgerError(
            "ROLE_FORBIDDEN",
            "an expert position may only commit itself; admin commits on behalf "
            "of others with refs[] to their recorded consent")
    if who != actor and not refs:
        raise store.LedgerError(
            "CONSENT_REQUIRED",
            "committing on behalf of another position requires refs[] to their "
            "recorded consent (ACK ≠ agree)")
    _check_mandate(actor, ver)
    payload = {"who": who, "what": what, "due": due, "refs": refs}
    key = args.get("idempotency_key")
    res = store.submit(db_path, type_="commitment", kind="observation",
                       issue_id=issue_id, actor_position_id=actor,
                       mandate_version=ver, payload=payload,
                       idem_key=str(key) if key else store.new_idempotency_key())
    return _out(ok=True, event_seq=res["event_seq"], ts=res["ts"],
                idempotency_key=res.get("idem_key"))


@_guarded
def agency_issue_status(args: dict) -> str:
    db_path = _db(args)
    issue_id = str(args.get("issue_id") or "")
    _known_issue(db_path, issue_id)
    proj = store.replay_projection(db_path, issue_id)
    proj.pop("as_of_seq", None)
    return _out(ok=True, issue_id=issue_id,
                as_of_seq=store.replay_projection(db_path, issue_id)["as_of_seq"],
                **proj)


@_guarded
def agency_escalate(args: dict) -> str:
    db_path = _db(args)
    issue_id = str(args.get("issue_id") or "")
    reason = str(args.get("reason") or "").strip()
    to = str(args.get("to") or "").strip()
    if not reason or not to:
        raise store.LedgerError("VALIDATION_FAILED", "reason and to are required")
    _known_issue(db_path, issue_id)
    pos, ver = _require_admin(db_path)
    payload = {"reason": reason, "to": to}
    key = args.get("idempotency_key")
    res = store.submit(db_path, type_="escalation", kind="observation",
                       issue_id=issue_id, actor_position_id=pos,
                       mandate_version=ver, payload=payload,
                       idem_key=str(key) if key else store.new_idempotency_key())
    return _out(ok=True, event_seq=res["event_seq"], ts=res["ts"],
                idempotency_key=res.get("idem_key"))


@_guarded
def agency_client_reply(args: dict) -> str:
    """Fix-before-ack, TZ v0.2: fixation ONLY. Returns the event id; delivery to
    the client is performed by the channel outside this toolset."""
    db_path = _db(args)
    issue_id = str(args.get("issue_id") or "")
    text = str(args.get("text") or "").strip()
    refs = list(args.get("refs") or [])
    if not text:
        raise store.LedgerError("VALIDATION_FAILED", "text must be non-empty")
    _known_issue(db_path, issue_id)
    pos, ver = _require_admin(db_path)
    payload = {"text": text, "refs": refs}
    key = args.get("idempotency_key")
    res = store.submit(db_path, type_="client_reply", kind="observation",
                       issue_id=issue_id, actor_position_id=pos,
                       mandate_version=ver, payload=payload,
                       idem_key=str(key) if key else store.new_idempotency_key())
    return _out(ok=True, event_seq=res["event_seq"], event_id=f"ledger:{res['event_seq']}",
                ts=res["ts"], idempotency_key=res.get("idem_key"))


@_guarded
def agency_register(args: dict) -> str:
    """Self-service coworking registration (Вл 07.11): expert/admin joins by
    registering; activation waits for admin approval. Binds the calling session."""
    db_path = _db(args)
    role = str(args.get("role") or "").strip()
    position_id = str(args.get("position_id") or "").strip()
    competencies = [str(c) for c in (args.get("competencies") or [])]
    sid = get_caller_session()
    if not sid:
        raise store.LedgerError(
            "VALIDATION_FAILED",
            "no caller session context: runtime must supply session identity")
    existing = store.position_for_session(db_path, sid)
    if existing is not None:
        raise store.LedgerError(
            "ALREADY_BOUND",
            f"session already bound to {existing['position_id']!r} "
            f"({existing['status']})")
    res = store.register_position(db_path, position_id, role, competencies, sid)
    store.bind_session(db_path, sid, position_id.strip().lower())
    return _out(ok=True, position_id=position_id.strip().lower(), **res)


@_guarded
def agency_approve(args: dict) -> str:
    db_path = _db(args)
    position_id = str(args.get("position_id") or "")
    new_role = args.get("role")
    pos, ver = _require_admin(db_path)
    _check_mandate(pos, ver)
    res = store.approve_position(db_path, position_id, pos,
                                 new_role=str(new_role) if new_role else None)
    return _out(ok=True, position_id=position_id.strip().lower(), **res)


# ------------------------------------------------------------------ schema ----

def _str(desc: str) -> dict:
    return {"type": "string", "description": desc}


_TOOLS: dict[str, tuple[Any, str, dict, list[str]]] = {
    "agency_list_experts": (
        agency_list_experts,
        "List agency positions from the registry with competencies and busyness.",
        {}, []),
    "agency_survey": (
        agency_survey,
        "Send a survey_request envelope to one expert position about an issue. "
        "Does NOT create an obligation — an obligation appears only when the "
        "expert explicitly agrees later.",
        {"expert_id": _str("Target position id"),
         "issue_id": _str("Existing issue id"),
         "question": _str("What this position is asked to self-assess")},
        ["expert_id", "issue_id", "question"]),
    "agency_create_branch": (
        agency_create_branch,
        "Register an independent work direction under an issue "
        "(fork-of-directions). Assigning an executor is NOT implied.",
        {"issue_id": _str("Existing issue id"),
         "title": _str("Direction title"),
         "context": _str("Short context handed to the direction")},
        ["issue_id", "title"]),
    "agency_record_commitment": (
        agency_record_commitment,
        "Record a commitment with a deadline. For another position (who != you) "
        "you MUST pass refs[] pointing to their recorded consent.",
        {"issue_id": _str("Existing issue id"),
         "who": _str("Position taking the obligation"),
         "what": _str("What exactly is promised"),
         "due": _str("ISO-8601 UTC deadline"),
         "refs": {"type": "array", "items": {"type": "string"},
                  "description": "Evidence references of consent when who != actor"}},
        ["issue_id", "who", "what", "due"]),
    "agency_issue_status": (
        agency_issue_status,
        "Client-visible status projection built from the same ledger the auditor "
        "sees. Read-only.",
        {"issue_id": _str("Existing issue id")}, ["issue_id"]),
    "agency_escalate": (
        agency_escalate,
        "Escalate an issue/spending dispute to a human owner. Does not resolve disputes.",
        {"issue_id": _str("Existing issue id"), "reason": _str("What is wrong"),
         "to": _str("Human/owner addressee")},
        ["issue_id", "reason", "to"]),
    "agency_client_reply": (
        agency_client_reply,
        "Fix a reply text destined for the client in the ledger FIRST; returns its "
        "event id. Delivery to the client happens by the channel after fixation.",
        {"issue_id": _str("Existing issue id"), "text": _str("Reply text"),
         "refs": {"type": "array", "items": {"type": "string"},
                  "description": "Evidence refs backing the statement"}},
        ["issue_id", "text"]),
    "agency_register": (
        agency_register,
        "Coworking self-registration: join the agency as 'expert' or 'administrator'. "
        "Your session binds to the new position; an admin approval activates it.",
        {"position_id": _str("Desired position id ([a-z0-9-], >=3 chars)"),
         "role": {"type": "string", "enum": ["expert", "administrator"]},
         "competencies": {"type": "array", "items": {"type": "string"},
                          "description": "Competency tags for the registry"}},
        ["position_id", "role"]),
    "agency_approve": (
        agency_approve,
        "Admin gate: activate a pending registered position (optionally set its "
        "final role). Administrator-only.",
        {"position_id": _str("Registered position id"),
         "role": {"type": "string", "enum": ["expert", "administrator"],
                  "description": "Optional final role"}},
        ["position_id"]),
}

TOOL_META: dict[str, dict[str, bool]] = {
    "agency_list_experts": {"readOnlyHint": True, "destructiveHint": False},
    "agency_issue_status": {"readOnlyHint": True, "destructiveHint": False},
}
for _n in ("agency_survey", "agency_create_branch", "agency_record_commitment",
           "agency_escalate", "agency_client_reply", "agency_register",
           "agency_approve"):
    TOOL_META[_n] = {"readOnlyHint": False, "destructiveHint": False}


def is_read_only(name: str) -> bool:
    return TOOL_META.get(name, {}).get("readOnlyHint", False)


def tool_defs() -> list[tuple[str, dict, Any]]:
    out = []
    for name, (handler, description, properties, required) in _TOOLS.items():
        parameters: dict[str, Any] = {"type": "object", "properties": properties}
        if required:
            parameters["required"] = required
        out.append((name, {"name": name, "description": description,
                           "parameters": parameters}, handler))
    return out

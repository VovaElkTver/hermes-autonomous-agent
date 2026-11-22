"""write-permission: mechanical write-permission discipline (DoFirst 22.11 §3.2 D2).

Owner doctrine (SOUL R13, 22.11): a write permission is granted only when the
owner's phrase carries BOTH an action verb over the content AND an object; a
permission is one-shot — spent once consumed; a cleanup is a terminal state
(no records after the deletion report until the next verb command). The verb
judgment is a semantic skill rule (write-permission skill); this plugin is the
MECHANICAL half:

- post-delete lock: after a successful `rm -r/-rf` the deleted target is locked
  for the session — write tools touching it are blocked (log-only: audited only);
- audit JSONL: every verdict line is the M.3/M.4 surface (unmapped writes);
- constraint injection: pre_llm_call surfaces active locks/constraints of the
  session as ephemeral turn context (channel: agent/turn_context.py, §3.1);
- session registry: <registry_dir>/<session>.json (locks + operator constraints).

Settings (plugins.entries.write-permission.settings, hot-read every call):
    enabled: true
    log_only: false
    audit_log: ~/.hermes/write-permission/audit.jsonl
    registry_dir: ~/.hermes/state/write-permission
    lock_scope: session        # or "turn": a lock binds its creating turn only
    features: {post_delete_lock, auto_lock_on_rm, inject_constraints, audit_log}

Zero settings (or a ctx without get_config) → defaults above, enforcing.
Locks are session-scoped by default (B-Q2 22.11); on_session_finalize clears
locks, keeps operator constraints.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import re
import sys
import threading

log = logging.getLogger(__name__)

_lock = threading.Lock()

# ── settings surface (plugins.entries.write-permission.settings) ───────────
_ctx = None
_FEATURES = ("post_delete_lock", "auto_lock_on_rm",
             "inject_constraints", "audit_log")
DEFAULT_AUDIT_LOG = "~/.hermes/write-permission/audit.jsonl"
DEFAULT_REGISTRY_DIR = "~/.hermes/state/write-permission"

# Direct-file-write tools: their args carry the path being written.
_PATH_ARG_TOOLS = {"write_file", "patch", "edit", "apply_patch", "str_replace_editor"}
# Free-text tools: a locked path matters only when the line carries a write hint
# (the gate fires on edits, not on mentions — 17.11 doctrine, reads stay free).
_TEXT_TOOLS = {"terminal", "execute_code"}
_WRITE_HINT_RE = re.compile(
    r"(>>?|\btee\b|\bsed\b[^|;\n]*\s-i|\btruncate\b|\bof=|\btouch\b"
    r"|\b(?:cp|mv|rm|install|ln|chmod|chown)\b|\bwrite_text\b|\bwrite_bytes\b"
    r"|\bpatch\b|open\s*\([^)]*['\"]{1}[wa])"
)
# `rm` with a recursive flag → the target enters a post-delete lock.
_RM_RECURSIVE_RE = re.compile(r"\brm\s+(?:-\w+\s+)*-(?:\w*r\w*|\w*f\w*r\w*)\s+(\S+)")


def _diag(msg: str) -> None:
    print(f"[write-permission] {msg}", file=sys.stderr)


def _settings() -> dict:
    """Hot-read the settings subtree. Never raises; degrades to enforcing defaults."""
    out: dict = {"enabled": True, "log_only": False,
                 "audit_log": DEFAULT_AUDIT_LOG, "registry_dir": DEFAULT_REGISTRY_DIR,
                 "lock_scope": "session",
                 "features": {f: True for f in _FEATURES}}
    get = getattr(_ctx, "get_config", None)
    if not callable(get):
        return out
    for key, fallback in (("enabled", out["enabled"]), ("log_only", out["log_only"]),
                          ("audit_log", out["audit_log"]),
                          ("registry_dir", out["registry_dir"]),
                          ("lock_scope", out["lock_scope"])):
        try:
            value = get(key, fallback)
        except Exception:
            value = fallback
        out[key] = fallback if value is None else value
    try:
        feats = get("features", None)
        if isinstance(feats, dict):
            for f in _FEATURES:
                if f in feats:
                    out["features"][f] = bool(feats[f])
    except Exception:
        pass
    return out


def _sid(kw: dict) -> str:
    return kw.get("session_id") or ""


def _registry_path(s: dict, sid: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", sid or "global")
    return os.path.join(os.path.expanduser(str(s["registry_dir"])), safe + ".json")


def _registry_read(s: dict, sid: str) -> dict | None:
    try:
        path = _registry_path(s, sid)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return None
        return {"locks": [l for l in data.get("locks", []) if isinstance(l, dict) and l.get("path")],
                "constraints": [c for c in data.get("constraints", []) if isinstance(c, str)]}
    except Exception as exc:
        log.warning("write-permission registry read failed for %r: %s", sid, exc)
        return None  # fail-open: unreadable registry = no active locks


def _registry_write(s: dict, sid: str, locks: list, constraints: list) -> None:
    try:
        path = _registry_path(s, sid)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = {"session": sid,
                "updated": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                "locks": locks, "constraints": constraints}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
    except Exception as exc:
        log.warning("write-permission registry write failed for %r: %s", sid, exc)


def _audit(s: dict, kind: str, tool_name: str, args: dict, kw: dict) -> None:
    """One JSONL line per verdict (the M.3/M.4 metric surface). Fail-soft."""
    if not s["features"]["audit_log"]:
        return
    try:
        path = os.path.expanduser(str(s["audit_log"]))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        text = ""
        if tool_name in ("terminal", "execute_code"):
            text = str((args or {}).get("command") or (args or {}).get("code") or "")[:300]
        rec = {"ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
               "kind": kind, "tool": tool_name, "session_id": _sid(kw),
               "turn_id": kw.get("turn_id") or "",
               "text": text or None}
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as exc:
        log.warning("write-permission audit write failed: %s", exc)


def _path_under(candidate: str, lock_path: str) -> bool:
    try:
        cand = os.path.abspath(os.path.expanduser(candidate)).lower()
        lock = os.path.abspath(os.path.expanduser(lock_path)).lower()
    except Exception:
        return False
    return cand == lock or cand.startswith(lock.rstrip(os.sep) + os.sep)


def _text_candidates(tool_name: str, args: dict, locks: list[str]):
    """Locked paths that a FREE-TEXT line actually writes to (edit hint required)."""
    text = str(args.get("command") or args.get("code") or "")
    for line in text.splitlines():
        if not _WRITE_HINT_RE.search(line):
            continue
        for lock in locks:
            if lock in line:
                yield lock, line


# ── hooks ────────────────────────────────────────────────────────────────────

def pre_tool_call(tool_name: str = "", args: dict | None = None, **kw):
    try:
        return _decide_locks(tool_name, args if isinstance(args, dict) else {}, kw)
    except Exception as exc:
        s = _settings()
        if s["enabled"] and s["log_only"]:
            _audit(s, "internal_error", tool_name,
                   args if isinstance(args, dict) else {}, kw)
            log.exception("write-permission pre_tool_call failed (log-only: allowing)")
            return None
        log.exception("write-permission pre_tool_call failed")
        return {"action": "block",
                "message": f"write-permission internal error: {exc}"}


def _decide_locks(tool_name: str, args: dict, kw: dict) -> dict | None:
    s = _settings()
    if not s["enabled"]:
        return None
    if not s["features"]["post_delete_lock"]:
        return None
    sid = _sid(kw)
    if not sid:
        return None  # locks are session-scoped; no identity → no opinion
    reg = _registry_read(s, sid)
    if not reg:
        return None
    turn = kw.get("turn_id") or ""
    locks = [l for l in reg["locks"]
             if s["lock_scope"] == "session" or l.get("turn_id", "") in ("", turn)]
    lock_paths = [str(l["path"]) for l in locks]
    if not lock_paths:
        return None

    def _verdict(path: str, detail: str) -> dict:
        return {"action": "block", "rule_key": "write-permission:post-delete-lock",
                "message": f"write-permission: {path} is under a post-delete lock "
                           f"(session {sid}). First reaction is a status line; "
                           f"writes only after a new owner command with a verb."
                           + (f" [{detail}]" if detail else "")}

    if tool_name in _PATH_ARG_TOOLS:
        cand = str(args.get("path") or args.get("file_path") or args.get("file") or "")
        if cand:
            for lock in lock_paths:
                if _path_under(cand, lock):
                    if s["log_only"]:
                        _audit(s, "would_block", tool_name, args, kw)
                        return None
                    _audit(s, "blocked", tool_name, args, kw)
                    return _verdict(lock, f"write target {cand}")
        return None
    if tool_name in _TEXT_TOOLS:
        for lock, line in _text_candidates(tool_name, args, lock_paths):
            if s["log_only"]:
                _audit(s, "would_block", tool_name, args, kw)
                return None
            _audit(s, "blocked", tool_name, args, kw)
            return _verdict(lock, f"line: {line.strip()[:120]}")
    return None


def post_tool_call(tool_name: str = "", args: dict | None = None, **kw):
    s = _settings()
    if not s["enabled"]:
        return
    args = args if isinstance(args, dict) else {}
    if kw.get("status") != "ok" or not s["features"]["auto_lock_on_rm"]:
        return
    if tool_name not in ("terminal", "execute_code"):
        return
    sid = _sid(kw)
    if not sid:
        return
    text = str(args.get("command") or args.get("code") or "")
    reg = _registry_read(s, sid)
    locks = reg["locks"] if reg else []
    constraints = reg["constraints"] if reg else []
    changed = False
    for m in _RM_RECURSIVE_RE.finditer(text):
        target = os.path.abspath(os.path.expanduser(m.group(1)))
        if not any(l["path"] == target for l in locks):
            locks.append({"path": target, "reason": "post-delete",
                          "created": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                          "turn_id": kw.get("turn_id") or ""})
            _audit(s, "lock-set", tool_name, args, kw)
            changed = True
    if changed:
        _registry_write(s, sid, locks, constraints)


def pre_llm_call(**kw):
    s = _settings()
    if not s["enabled"] or not s["features"]["inject_constraints"]:
        return {}
    sid = _sid(kw)
    if not sid:
        return {}
    reg = _registry_read(s, sid)
    if not reg:
        return {}
    turn = kw.get("turn_id") or ""
    locks = [l for l in reg["locks"]
             if s["lock_scope"] == "session" or l.get("turn_id", "") in ("", turn)]
    lines = []
    for l in locks:
        reason = l.get("reason") or "lock"
        lines.append(f"- post-delete lock: {l['path']} ({reason}) — first reaction is a "
                     "status line; writes only after a new owner command with a verb")
    for c in reg["constraints"]:
        lines.append(f"- owner constraint: {c}")
    if not lines:
        return {}
    return {"context": "[write-permission] Active constraints this session:\n" + "\n".join(lines)}


def on_session_finalize(**kw):
    """Session teardown: clear this session's locks; keep operator constraints."""
    s = _settings()
    sid = _sid(kw)
    if not sid:
        return
    reg = _registry_read(s, sid)
    if reg:
        _registry_write(s, sid, [], reg["constraints"])


def register(ctx):
    global _ctx
    _ctx = ctx  # settings surface; hooks stay registered even when the master switch
    # is off, so a config flip (enabled/log_only/features) takes effect without a restart
    ctx.register_hook("pre_tool_call", pre_tool_call)
    ctx.register_hook("post_tool_call", post_tool_call)
    ctx.register_hook("pre_llm_call", pre_llm_call)
    ctx.register_hook("on_session_finalize", on_session_finalize)

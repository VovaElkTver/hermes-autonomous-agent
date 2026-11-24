"""fork-guardrails: policy hooks for Hermes fork work.

v0.3.0 (22.11, DoFirst §3.2 D0): per-feature toggles + log-only mode.
The 27.10 crash-loop recovery could only disable the plugin WHOLE, because
register() wired all five hooks unconditionally and the config entry carried
no per-feature surface — "all or nothing", so the victim of a boot loop got
killed together with the culprit. The settings subtree fixes that:

  plugins.entries.fork-guardrails.settings:
    enabled: bool        master switch (default true)
    log_only: bool       dry-run: verdicts computed + audited, never enforced (default false)
    audit_log: str       JSONL verdict log path (default ~/.hermes/fork-guardrails/audit.jsonl)
    features:           one bool per guard (all default true):
      retry_guard        identical-call failure streak block (3x per turn)
      delegation_gate    delegate_task only after an explicit user ask
      force_push_block   force-push to main/master is forbidden
      outbound_approve   git-push / gh-publish / registry-push / rm-recursive need approval
      date_context       pre_llm_call "Current date" context injection

Zero settings (or a ctx without get_config) → all features ON, enforcing,
audit ON: byte-compatible with the 0.2.x behavior. Settings are re-read on
every invocation (hot — a config flip needs no gateway restart).

Every verdict (enforced or log-only would-be) is appended as one JSONL line
to audit_log: that stream is the metric surface (e.g. un-sanctioned writes)
the 22.11 critic package requires (DoFirst М.4).

v0.4.0 (23.11, after incident dgx98): remote writes over ssh/scp/rsync/sftp are
detected and recorded to write_ledger.jsonl (host, targets, best-effort md5
before; md5 after lands asynchronously from a daemon thread on success).
v0.4.1 (23.11, owner word): the allowlist BLOCK (M2) and the child scope note
(M4) are REMOVED — nothing in this plugin may require a host allowlist. The
ledger is pure audit: it never blocks and needs no list.

v0.5.0 (23.11, incident dgx98 post-mortem — mechanism, not prompt lists):
the intervention barrier and the remote-write mandate, at the executor:

- auth_epoch: a new owner message bumps a persisted turn counter; turn-scoped
  grants die with the turn (on_session_end) and a new message revokes them —
  "a new message invalidates the old authority to act", mechanically.
- mandate_gate: remote write/delete/export classes (ssh/sftp verb commands,
  scp/rsync in destination position, export = remote source / local sink)
  need an ACTIVE turn grant, else an approval-gate verdict with the action
  CONTRACT (basis / target / target state / boundaries / verification /
  uncertainty). Owner approval = one-shot permission for that call; a
  successful call grants the same session+host+class for the REST OF THE
  TURN only (pre-agreed-plan semantics: no card per step of one turn, but
  the next owner message voids all of it).
- No host allowlist anywhere (owner word 23.11 stands): grants are
  task-scoped, owner-confirmed, epoch-bound — not a static list.
- Op journal: ledger lines carry op_id / turn / op_class in addition to the
  M6 before/after md5 + artifact (host/path) chain.
- Detecting one's own error creates NO permission to fix it: the gate does
  not special-case "repair" motives — every remote mutation needs the card.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
import re
import shlex
import subprocess
import threading
import time
import uuid
from collections import defaultdict

log = logging.getLogger(__name__)

MAX_IDENTICAL_FAILURES = 3
DELEGATION_TOOL = "delegate_task"          # tools/delegate_tool.py
SHELL_TOOLS = {"terminal", "execute_code"}  # execute_code can spawn subprocesses too

# Patterns are a speed bump for the model, not a sandbox (see #73338 discussion).
FORCE_PUSH_PROTECTED = re.compile(
    r"git\s+push\b(?=.*(?:\s--force(?!-with-lease)\b|\s-[a-zA-Z]*f\b|\s\+))(?=.*\b(?:main|master)\b)", re.DOTALL)
OUTBOUND = {
    "git-push": re.compile(r"\bgit\s+push\b"),
    "gh-publish": re.compile(r"\bgh\s+(?:pr\s+(?:create|merge)|release\s+create)\b"),
    "registry-push": re.compile(r"\b(?:docker|podman)\s+push\b|\btwine\s+upload\b|\bnpm\s+publish\b"),
    "rm-recursive": re.compile(r"\brm\s+(?:-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r|-r\s+-f|-f\s+-r|--recursive)\b"),
}
DELEGATION_ASK = re.compile(r"\b(?:sub-?agents?|parallel agents?|delegate|delegation)\b"
                            r"|(?:субагент\w*|суб-агент\w*|делегат\w*|делегироват\w*)",
                            re.IGNORECASE)
DELEGATION_NEG = re.compile(r"\b(?:don'?t|do not|no|never|without)\s+(?:\w+\s+){0,2}(?:sub-?agents?|delegat\w*)"
                            r"|\b(?:без|никаких|не)\s+(?:\w+\s+){0,2}(?:субагент\w*|делегат\w*)",
                            re.IGNORECASE)

_lock = threading.Lock()                     # tool batches run on a thread pool
_failures: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))
_delegation_ok: set[str] = set()

# ── v0.3.0 settings surface (plugins.entries.fork-guardrails.settings) ─────
_ctx = None
_FEATURES = ("retry_guard", "delegation_gate", "force_push_block",
             "outbound_approve", "date_context",
             "write_ledger", "auth_epoch", "mandate_gate", "checkout_lease")
DEFAULT_AUDIT_LOG = "~/.hermes/fork-guardrails/audit.jsonl"
_TURN_STATE_FILE = "auth_state.json"


def _settings() -> dict:
    """Hot-read the settings subtree. Never raises; degrades to 0.2.x defaults."""
    out: dict = {"enabled": True, "log_only": False, "audit_log": DEFAULT_AUDIT_LOG,
                 "features": {f: True for f in _FEATURES}}
    get = getattr(_ctx, "get_config", None)
    if not callable(get):
        return out
    for key, fallback in (("enabled", out["enabled"]), ("log_only", out["log_only"]),
                          ("audit_log", out["audit_log"])):
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


def _audit(kind: str, decision: dict, tool_name: str, args: dict, kw: dict, s: dict) -> None:
    """One JSONL line per verdict. Audit failure degrades to a log line, never raises."""
    try:
        path = os.path.expanduser(str(s.get("audit_log") or DEFAULT_AUDIT_LOG))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        text = ""
        if tool_name in SHELL_TOOLS:
            text = str((args or {}).get("command") or (args or {}).get("code") or "")[:300]
        rec = {
            "ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "kind": kind,
            "action": (decision or {}).get("action"),
            "rule_key": (decision or {}).get("rule_key"),
            "tool": tool_name,
            "session_id": _sid(kw),
            "turn_id": kw.get("turn_id") or "",
            "text": text or None,
        }
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as exc:
        log.warning("fork-guardrails audit write failed: %s", exc)


def _sid(kw: dict) -> str:
    return kw.get("session_id") or kw.get("task_id") or "_global"


def _sig(tool: str, args: dict) -> str:
    return hashlib.sha1(f"{tool}:{json.dumps(args, sort_keys=True, default=str)}".encode()).hexdigest()


def _shell_text(tool_name: str, args: dict) -> str:
    if tool_name == "terminal":
        return str(args.get("command") or args.get("code") or "")
    if tool_name == "execute_code":
        return str(args.get("code") or "")
    return ""


# ── v0.4.1 M6: write ledger (audit trail only; no list, no blocking) ───────
SSH_OPTS_WITH_VALUE = {"-p", "-i", "-F", "-o", "-l", "-c", "-b", "-D", "-J", "-L",
                       "-R", "-W", "-m", "-Q", "-w", "-E", "-e"}
# transport verb, not a prefix of a bigger word (ssh-keygen, rsync:// stay out)
_TRANSPORT_RE = re.compile(r"\b(?:ssh|scp|rsync|sftp)\b(?![-\w])")
# write-class commands run remotely; '> ' redirect counts (bare '2>' stderr does not)
_WRITE_VERB_RE = re.compile(
    r"\b(?:rm|mv|cp|tee|install|mkdir|rmdir|touch|truncate|chmod|chown|dd|kill|pkill|killall"
    r"|usermod|userdel|crontab|systemctl\s+(?:stop|start|restart|enable|disable|mask|unmask|reload|daemon-)"
    r"|apt(?:-get)?\s+(?:install|remove|purge|autoremove)|docker\s+(?:run|compose\s+up))\b"
    r"|(?<![0-9&])>>?(?![=&])")
_REMOTE_EP_RE = re.compile(r"^(?:[\w.+-]+@)?(\[[0-9A-Fa-f:.]+\]|(?![A-Za-z]:)[\w.-]+):")
_ABS_PATH_RE = re.compile(r"(?<![\w~$@])/[\w@.+-]+(?:/[\w@.+-]+)*")

_pending_writes: dict[tuple[str, str, str], dict] = {}
# v0.5.0: turn-scoped mandate grants (session_id -> set of (host, op_class)).
# In-memory by design: a crash/restart loses grants (the safe side — nothing
# resurrects). The PERSISTED half is only the turn counter (auth_state.json),
# so op-journal numbering survives restarts.
_turn_grants: dict[str, set[tuple[str, str]]] = {}
# (sid, turn, sig) -> list[(host, op_class)] for calls that passed the gate
# via an owner approval; post_tool_call(status=ok) converts them into grants.
_gated_calls: dict[tuple[str, str, str], list[tuple[str, str]]] = {}
# (sid, turn, sig) -> short note for context injection (bounded per session)
_mandate_events: dict[tuple[str, str], list[str]] = defaultdict(list)


def _plugin_dir(s: dict) -> str:
    return os.path.dirname(os.path.expanduser(str(s.get("audit_log") or DEFAULT_AUDIT_LOG)))


def _remote_write_hosts(text: str) -> set[str]:
    """Hosts this shell text WRITES to over a remote transport (best-effort, speed bump not sandbox).
    ssh/sftp: any write verb (or bare shell) after the host. scp/rsync: remote endpoint in
    DESTINATION position (last positional). Downloads (remote source) are reads and never gate."""
    hosts: set[str] = set()
    for m in _TRANSPORT_RE.finditer(text):
        verb = text[m.start():m.end()]
        tail = re.split(r"[;&|\n]", text[m.end():], maxsplit=1)[0]
        try:
            toks = shlex.split(tail)
        except ValueError:
            # unbalanced quotes in the surrounding text (e.g. a print("...") probe):
            # fall back to naive tokenization — a gate that skips on parse errors gates nothing
            toks = tail.split()
        pos: list[str] = []
        i = 0
        while i < len(toks):
            t = toks[i]
            if t.startswith("-") and len(t) > 1:
                i += 2 if (t in SSH_OPTS_WITH_VALUE and i + 1 < len(toks)) else 1
                continue
            pos.append(t)
            i += 1
        if not pos:
            continue
        if verb in ("ssh", "sftp"):
            host, rest = pos[0], " ".join(pos[1:])
            if not re.fullmatch(r"(?:[\w.+-]+@)?[\w.-]+", host):
                continue
            if verb == "sftp" or not rest or _WRITE_VERB_RE.search(rest):
                hosts.add(host)
        else:
            dest = pos[-1]
            ep = _REMOTE_EP_RE.match(dest)
            if ep:
                hosts.add(ep.group(1))
    return hosts


def _remote_write_targets(text: str) -> list[str]:
    """Absolute-path-looking write targets in the remote segments (best-effort, ≤8)."""
    out: list[str] = []
    for m in _TRANSPORT_RE.finditer(text):
        tail = re.split(r"[;&|\n]", text[m.end():], maxsplit=1)[0]
        for p in re.finditer(r"(?<![\w~$@:])/[\w@.+-]+(?:/[\w@.+-]+)*", tail):
            path = p.group(0)
            if path != "/dev/null" and path not in out:
                out.append(path)
    return out[:8]


# ── v0.5.0 M-mechanism: op classes, export detection, turn state, grants ────
_DELETE_VERB_RE = re.compile(r"\b(?:rm|rmdir|mv|truncate|dd)\b")
_LOCAL_SINK_RE = re.compile(r"(?<![0-9&])>(?![=>])\s*\S")


def _remote_write_classes(text: str) -> dict[str, set[str]]:
    """host -> {"write"} / {"delete"} classes this shell text performs REMOTELY.

    Reuses _remote_write_hosts' reach (transport + write-verb or bare shell) and
    splits the class: rm/mv/truncate/rmdir/dd remotely = delete, anything else
    write. Reads stay OUT (no verb, no redirect) — the gate fires on edits,
    not on mentions (17.11 doctrine)."""
    out: dict[str, set[str]] = {}
    for host in _remote_write_hosts(text):
        cls = {"write"}
        m = _TRANSPORT_RE.search(text)
        if m and _DELETE_VERB_RE.search(re.split(r"[;&|\n]", text[m.end():], maxsplit=1)[0]):
            cls = {"delete", "write"}
        out[host] = cls
    return out


def _remote_export_hosts(text: str) -> set[str]:
    """Hosts this shell text EXPORTS data FROM (best-effort, speed bump not sandbox).

    - scp/rsync with a REMOTE endpoint in SOURCE position (not last positional);
    - sftp `get` commands;
    - `ssh <host> <cmd>` whose tail carries a local sink redirect (`> file`) with
      no remote write verb — the remote side only read, the data landed locally.
    """
    hosts: set[str] = set()
    for m in _TRANSPORT_RE.finditer(text):
        verb = text[m.start():m.end()]
        tail = re.split(r"[;&|\n]", text[m.end():], maxsplit=1)[0]
        try:
            toks = shlex.split(tail)
        except ValueError:
            toks = tail.split()
        pos: list[str] = []
        i = 0
        while i < len(toks):
            t = toks[i]
            if t.startswith("-") and len(t) > 1:
                i += 2 if (t in SSH_OPTS_WITH_VALUE and i + 1 < len(toks)) else 1
                continue
            pos.append(t)
            i += 1
        if verb in ("scp", "rsync") and len(pos) >= 2:
            for p in pos[:-1]:  # sources: every positional except the destination
                ep = _REMOTE_EP_RE.match(p)
                if ep:
                    hosts.add(ep.group(1))
        elif verb == "sftp":
            low = " ".join(pos[1:]).lower()
            if re.search(r"\bget\b", low):
                hosts.add(pos[0])
        elif verb == "ssh" and pos:
            rest = " ".join(pos[1:])
            if not _WRITE_VERB_RE.search(rest) and _LOCAL_SINK_RE.search(rest):
                hosts.add(pos[0])
    return hosts


def _load_turn_state(s: dict) -> dict:
    try:
        path = os.path.join(_plugin_dir(s), _TURN_STATE_FILE)
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return {"turn": int(data.get("turn", 0)), "sessions": data.get("sessions") or {}}
    except Exception:
        pass
    return {"turn": 0, "sessions": {}}


def _commit_turn_state(s: dict, st: dict) -> None:
    try:
        path = os.path.join(_plugin_dir(s), _TURN_STATE_FILE)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(st, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as exc:
        log.warning("fork-guardrails turn-state write failed: %s", exc)


def _bump_on_new_message(s: dict, sid: str, kw: dict) -> int | None:
    """v0.5.0 intervention barrier: a NEW owner message bumps the persisted turn
    counter — old turns' grants are already dead (on_session_end cleared them);
    the bump is the audit/revocation record. Returns the new turn or None."""
    msg = (kw.get("user_message") or "").strip()
    if not msg:
        return None
    h = hashlib.sha1(msg.encode("utf-8", "replace")).hexdigest()[:16]
    with _lock:
        st = _load_turn_state(s)
        last = (st.get("sessions") or {}).get(sid) or {}
        if last.get("msg_hash") == h:
            return int(st.get("turn", 0))
        st["turn"] = int(st.get("turn", 0)) + 1
        st.setdefault("sessions", {})[sid] = {"msg_hash": h, "turn": st["turn"]}
        _commit_turn_state(s, st)
        _audit("epoch-bump", {"action": "epoch-bump",
                   "message": f"new owner message — previous turn's mandates void (turn {st['turn']})"},
                "", {}, kw, s)
        return st["turn"]


def _mandate_note(s: dict, sid: str, host_cls: list[tuple[str, str]], turn: int | None) -> None:
    """One bounded context line per gate event (the model-awareness layer; the
    verdict is the mechanism, this is not a substitute for it)."""
    desc = ", ".join(f"{c}@{h}" for h, c in host_cls[:4])
    note = f"- mandate-gate turn {turn}: no permission for {desc} without owner approval; " \
           "new owner message voids the previous turn's grants"
    with _lock:
        ev = _mandate_events[(sid, str(turn) if turn is not None else "")]
        if note not in ev:
            ev.append(note)
            del ev[:-3]


def _ssh_md5(host: str, target: str):
    """Read-only remote md5; never raises. None when it cannot be had."""
    try:
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host, "md5sum", "--", target],
            capture_output=True, text=True, timeout=12, check=False)
        if r.returncode == 0 and r.stdout.split():
            return r.stdout.split()[0]
    except Exception:
        return None
    return None


def _now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _ledger(s: dict, rec: dict) -> None:
    """Append-only write ledger next to the audit log. Never raises (M6)."""
    try:
        if not s["features"].get("write_ledger", True):
            return
        path = os.path.join(_plugin_dir(s), "write_ledger.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": _now_iso(), **rec}, ensure_ascii=False) + "\n")
    except Exception as exc:
        log.warning("fork-guardrails ledger write failed: %s", exc)


def _remote_write_record(text: str, tool_name: str, args: dict, kw: dict, s: dict) -> None:
    """M6 ledger preflight + v0.5.0 op-journal fields. Pure audit: no allowlist,
    no verdict, never blocks (the mandate gate is a SEPARATE feature)."""
    hosts = set(_remote_write_hosts(text)) | _remote_export_hosts(text)
    if not hosts:
        return
    targets = _remote_write_targets(text)
    single = (next(iter(hosts)), targets[0]) if (len(hosts) == 1 and len(targets) == 1 and "*" not in targets[0]) else None
    md5, note = None, "skipped: not a single exact target"
    if single:
        md5 = _ssh_md5(*single)
        note = None if md5 else "read failed"
    sid = _sid(kw)
    op_classes = sorted({c for cs in _remote_write_classes(text).values() for c in cs}
                        | ({"export"} if _remote_export_hosts(text) else set()))
    op_id = uuid.uuid4().hex[:12]
    turn_now = int(_load_turn_state(s).get("turn", 0))
    _ledger(s, {"kind": "remote_write", "hosts": sorted(hosts), "targets": targets,
                "cmd": text[:300], "tool": tool_name, "session_id": sid, "turn_id": kw.get("turn_id") or "",
                "md5_before": md5, "md5_note": note,
                "op_id": op_id, "op_class": op_classes, "turn": turn_now})
    with _lock:
        _pending_writes[(sid, kw.get("turn_id") or "", _sig(tool_name, args or {}))] = {
            "hosts": sorted(hosts), "cmd": text[:300], "single": single,
            "op_id": op_id, "op_class": op_classes, "turn": turn_now}
    return


def _settle_pending_write(tool_name: str, args: dict, kw: dict, s: dict, status: str) -> None:
    """M6 post-fact line for a gated remote write; md5-after asynchronously when a single target is known."""
    key = (_sid(kw), kw.get("turn_id") or "", _sig(tool_name, args or {}))
    with _lock:
        ctxp = _pending_writes.pop(key, None)
    if ctxp is None or not s["features"].get("write_ledger", True):
        return
    _ledger(s, {"kind": "remote_write_result", "status": status, "hosts": ctxp["hosts"],
                "cmd": ctxp["cmd"], "tool": tool_name, "session_id": _sid(kw), "turn_id": kw.get("turn_id") or "",
                "op_id": ctxp.get("op_id"), "op_class": ctxp.get("op_class"), "turn": ctxp.get("turn")})
    if status == "ok" and ctxp.get("single"):
        host, target = ctxp["single"]

        def _after():
            _ledger(s, {"kind": "remote_md5_after", "host": host, "target": target,
                        "md5": _ssh_md5(host, target),
                        "op_id": ctxp.get("op_id"), "op_class": ctxp.get("op_class"), "turn": ctxp.get("turn")})
        threading.Thread(target=_after, daemon=True).start()


def pre_llm_call(**kw):
    s = _settings()
    if not s["enabled"]:
        return {}
    sid, msg = _sid(kw), kw.get("user_message") or ""
    if s["features"]["delegation_gate"]:
        with _lock:
            if DELEGATION_NEG.search(msg):
                _delegation_ok.discard(sid)
            elif DELEGATION_ASK.search(msg):
                _delegation_ok.add(sid)          # consumed by the next successful delegation (post_tool_call)
    turn = None
    if s["features"]["auth_epoch"]:
        turn = _bump_on_new_message(s, sid, kw)   # None when the message is a repeat of the current one
    if not s["features"]["date_context"]:
        return {}
    context = f"Current date: {datetime.datetime.now().astimezone():%Y-%m-%d %H:%M %Z}. Verify post-cutoff facts with tools."
    mand: list[str] = []
    if s["features"]["mandate_gate"]:
        with _lock:
            gr = _turn_grants.get(sid, set())
            ev = _mandate_events.get((sid, str(turn) if turn is not None else ""), [])
        if gr:
            mand.append("[mandate-gate] active this turn (owner-approved): "
                        + "; ".join(f"{c} on {h}" for h, c in sorted(gr)))
        if ev:
            mand.extend(ev)
        if mand:
            context += "\n" + "\n".join(mand[:4])
    return {"context": context}


def _decide(tool_name: str, args: dict, kw: dict):
    s = _settings()
    if not s["enabled"]:
        return None
    log_only = bool(s["log_only"])

    def _verdict(decision: dict) -> dict | None:
        """Enforce unless log-only; both modes leave an audit line (the metric surface)."""
        if log_only:
            kind = "would_block" if decision["action"] == "block" else "would_approve"
            _audit(kind, decision, tool_name, args, kw, s)
            log.info("fork-guardrails log-only %s: %s", kind,
                     decision.get("rule_key") or decision.get("message"))
            return None
        kind = "blocked" if decision["action"] == "block" else "approve"
        _audit(kind, decision, tool_name, args, kw, s)
        return decision

    sid, turn = _sid(kw), kw.get("turn_id") or ""
    with _lock:
        if s["features"]["retry_guard"] and _failures[(sid, turn)][_sig(tool_name, args)] >= MAX_IDENTICAL_FAILURES:
            return _verdict({"action": "block",
                            "message": f"Identical {tool_name} call failed {MAX_IDENTICAL_FAILURES}x this turn; "
                                       "change approach or ask the user."})
        if s["features"]["delegation_gate"] and tool_name == DELEGATION_TOOL and sid not in _delegation_ok:
            return _verdict({"action": "block",
                            "message": "Delegation is only allowed when the user asks for subagents."})
    text = _shell_text(tool_name, args)
    if s["features"].get("checkout_lease", True):
        _lv = _checkout_gate(tool_name, args, text, kw, s, _verdict)
        if _lv is not None:
            return _lv
    if text:
        if s["features"]["force_push_block"] and FORCE_PUSH_PROTECTED.search(text):
            return _verdict({"action": "block",
                            "message": "Force-push to main/master is forbidden in this fork."})
        if s["features"]["write_ledger"]:
            _remote_write_record(text, tool_name, args, kw, s)
        if s["features"]["outbound_approve"]:
            for key, pat in OUTBOUND.items():
                if pat.search(text):
                    return _verdict({"action": "approve", "message": f"{tool_name}: {text[:300]}",
                                     "rule_key": f"fork-guardrails:{key}"})
        if s["features"]["mandate_gate"]:
            needed = [(h, c) for h, cs in _remote_write_classes(text).items() for c in cs]
            needed += [(h, "export") for h in _remote_export_hosts(text)]
            if needed:
                sid = _sid(kw)
                with _lock:
                    grants = set(_turn_grants.get(sid, set()))
                missing = [(h, c) for h, c in needed if (h, c) not in grants]
                if missing:
                    hosts_s = ", ".join(sorted({h for h, _ in missing}))
                    cls_s = ", ".join(sorted({c for _, c in missing}))
                    turn_now = int(_load_turn_state(s).get("turn", 0))
                    _mandate_note(s, sid, missing, turn_now)
                    if not log_only:
                        with _lock:
                            _gated_calls[(sid, kw.get("turn_id") or "", _sig(tool_name, args or {}))] = missing
                    return _verdict({
                        "action": "approve", "rule_key": "fork-guardrails:mandate-gate",
                        "message": (
                            f"[mandate-gate] no permission for {cls_s} on {hosts_s} in the current turn. "
                            "A new owner message voids the previous turn's grants, and detecting one's "
                            "own error creates no permission to repair it. FIRST post the contract in "
                            "chat — basis (which owner utterance permits this; if none, ask the owner "
                            "instead of acting), target (host + path), change, target state (if the "
                            "resulting state is unknown, the operation is not permitted), boundaries, "
                            "verification — then re-issue the call; the owner's approval covers THIS "
                            "action only, for this turn.")})
    return None


def pre_tool_call(tool_name: str = "", args: dict | None = None, **kw):
    try:
        return _decide(tool_name, args if isinstance(args, dict) else {}, kw)
    except Exception as exc:                 # host fails OPEN on exceptions
        s = _settings()
        if s["enabled"] and s["log_only"]:
            # log-only must stay zero-impact: diagnose, audit, allow
            _audit("internal_error",
                   {"action": "block", "message": f"fork-guardrails internal error: {exc}"},
                   tool_name, args if isinstance(args, dict) else {}, kw, s)
            log.exception("fork-guardrails pre_tool_call failed (log-only: allowing)")
            return None
        log.exception("fork-guardrails pre_tool_call failed")
        return {"action": "block", "message": f"fork-guardrails internal error: {exc}"}


def post_tool_call(tool_name: str = "", args: dict | None = None, **kw):
    s = _settings()
    if not s["enabled"]:
        return
    sig = _sig(tool_name, args if isinstance(args, dict) else {})
    key = (_sid(kw), kw.get("turn_id") or "")
    status = kw.get("status")
    with _lock:
        if status == "error" and s["features"]["retry_guard"]:
            _failures[key][sig] += 1
        elif status == "ok":
            if s["features"]["retry_guard"]:
                _failures[key].pop(sig, None)
            if s["features"]["delegation_gate"] and tool_name == DELEGATION_TOOL:
                # One-shot: the user's "use subagents" authorises THAT delegation, not the
                # whole session. After a successful dispatch the flag is consumed; a new
                # round needs a fresh ask (negation in pre_llm_call remains an early revoke).
                _delegation_ok.discard(_sid(kw))
        # status == "blocked": a plugin veto, not a tool failure — don't count it
    if status in ("ok", "error") and s["features"]["write_ledger"]:
        _settle_pending_write(tool_name, args if isinstance(args, dict) else {}, kw, s, status)
    if status == "ok" and s["features"]["mandate_gate"]:
        # A successful owner-approved call grants session+host+class for the REST
        # OF THIS TURN only (pre-agreed-plan semantics); on_session_end voids it.
        gkey = (_sid(kw), kw.get("turn_id") or "", sig)
        with _lock:
            granted = _gated_calls.pop(gkey, None)
        if granted:
            with _lock:
                _turn_grants.setdefault(_sid(kw), set()).update(granted)
            _audit("mandate-grant", {"action": "grant",
                                      "message": f"{_sid(kw)}: {[f'{c} on {h}' for h, c in granted]} for the rest of this turn"},
                   tool_name, args if isinstance(args, dict) else {}, kw, s)


# ── v0.5.2 checkout lease: one checkout, one session (ToDo §4, Вл 21.11) ───
CHECKOUT_TTL_S = 1800.0
_GIT_MUTATING_RE = re.compile(
    r"\bgit\b[^\n;|&]*?\b(checkout|switch|commit|add|merge|rebase|reset|stash|clean|apply|cherry-pick|restore)\b")
_FILE_WRITE_TOOLS = {"write_file", "patch"}
_REPO_CACHE: dict[str, str] = {}


def _git_target_dir(text: str) -> str:
    """Best-effort target dir of a git mutation: `-C <path>` wins, else the
    last `cd <path>` before the verb. Constructed-command bypasses remain
    possible (cooperative wall, visible in audit)."""
    m = re.search(r"git\b[^\n;|&]*?\s-C\s+([^\s;|&]+)", text)
    if m:
        return os.path.expanduser(m.group(1))
    cd = [x for x in re.findall(r"(?:^|[;&|]|&&)\s*cd\s+([^\s;&|]+)", text)]
    return os.path.expanduser(cd[-1]) if cd else ""


def _repo_root_for(path: str) -> str:
    """realpath of the git toplevel containing ``path``; "" when not in a repo."""
    path = os.path.expanduser(str(path or ""))
    probe = path if os.path.isdir(path) else (os.path.dirname(path) or ".")
    key = os.path.abspath(probe)
    hit = _REPO_CACHE.get(key)
    if hit is not None:
        return hit
    root = ""
    try:
        r = subprocess.run(["git", "-C", key, "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=5, check=False)
        root = os.path.realpath(r.stdout.strip()) if r.returncode == 0 else ""
    except Exception:
        root = ""
    _REPO_CACHE[key] = root
    return root


def _lease_file(s: dict, repo: str) -> str:
    import hashlib
    digest = hashlib.sha1(os.path.realpath(repo).encode()).hexdigest()[:16]
    return os.path.join(_plugin_dir(s), "leases", digest + ".json")


def _read_lease(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.loads(fh.read())
        return data if isinstance(data, dict) and data.get("session_id") else None
    except Exception:
        return None


def _git_out(repo: str, *argv: str) -> str:
    try:
        r = subprocess.run(["git", "-C", repo, *argv], capture_output=True, text=True,
                           timeout=10, check=False)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def _checkout_gate(tool_name: str, args: dict, text: str, kw: dict, s: dict, _verdict):
    """Lease enforcement at the executor (prompt rules were already proven
    insufficient — this is the layer where the mixing actually happened).

    log-only mode computes the verdict but writes NO lease state (shadow period
    must not mint fake owners). Delegated children with their own session ids
    will hit the parent's lease — during shadow that is visible, then either a
    family-key or worktrees-by-default settles it."""
    sid = _sid(kw)
    if not sid or sid == "unknown":
        return None
    repo = ""
    if tool_name in _FILE_WRITE_TOOLS:
        repo = _repo_root_for(str((args or {}).get("path") or ""))
    elif tool_name in ("terminal", "execute_code", "search_files", "task_queue"):
        if text and _GIT_MUTATING_RE.search(text):
            probe = _git_target_dir(text) or str((args or {}).get("cwd") or os.getcwd())
            repo = _repo_root_for(probe)
    if not repo:
        return None
    lf = _lease_file(s, repo)
    lease = _read_lease(lf)
    now = time.time()
    holder = str((lease or {}).get("session_id") or "")
    if lease and holder == sid:
        if not s["log_only"]:
            lease["ts"] = now
            try:
                with open(lf, "w", encoding="utf-8") as fh:
                    json.dump(lease, fh)
            except Exception:
                pass
        return None
    if lease and (now - float(lease.get("ts") or 0)) < CHECKOUT_TTL_S:
        return _verdict({"action": "block", "rule_key": "fork-guardrails:checkout-lease",
                         "message": f"checkout {repo} is leased to session {holder!r} "
                                    f"(branch {lease.get('branch')!r}, active "
                                    f"{int(now - float(lease['ts']))}s ago). Do not write into another "
                                    "session's checkout: create your own `git worktree add` branch or "
                                    "return findings; two sessions on one index mix commit histories."})
    if lease:  # stale lease
        dirty = [l[3:] for l in _git_out(repo, "status", "--porcelain=v1").splitlines() if l.strip()][:20]
        if dirty:
            return _verdict({"action": "block", "rule_key": "fork-guardrails:checkout-dirty",
                             "message": f"checkout {repo}: stale lease of {holder!r}, but the tree is "
                                        f"dirty ({len(dirty)} paths, e.g. {', '.join(dirty[:4])}). The old "
                                        "owner must commit/clean its work first; stealing a dirty tree is "
                                        "exactly how histories got mixed 23.11/24.11."})
    if s["log_only"]:
        return None
    os.makedirs(os.path.dirname(lf), exist_ok=True)
    rec = {"session_id": sid, "repo": repo, "branch": _git_out(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() or "?",
           "ts": now, "took_over_from": holder or None, "tool": tool_name}
    try:
        with open(lf, "w", encoding="utf-8") as fh:
            json.dump(rec, fh)
    except Exception:
        return None
    _audit("lease_acquire", {"action": "allow", "rule_key": "fork-guardrails:checkout-lease",
                             "message": f"{sid} -> {repo}@{rec['branch']}"}, tool_name, args, kw, s)
    return None


def _release_leases(sid: str, s: dict) -> None:
    """Real session teardown: drop every lease this session owns."""
    d = os.path.join(_plugin_dir(s), "leases")
    try:
        names = os.listdir(d)
    except OSError:
        return
    for name in names:
        path = os.path.join(d, name)
        lease = _read_lease(path)
        if lease and lease.get("session_id") == sid:
            try:
                os.unlink(path)
            except OSError:
                pass


def on_session_end(**kw):
    """Fires after EVERY turn (despite the name): drop that turn's retry counters
    and its mandate grants (the intervention barrier — the next owner message
    starts with a clean slate of permissions)."""
    key = (_sid(kw), kw.get("turn_id") or "")
    with _lock:
        _failures.pop(key, None)
        for k in [k for k in _pending_writes if k[:2] == key]:
            _pending_writes.pop(k, None)
        _turn_grants.pop(key[0], None)
        for k in [k for k in _gated_calls if k[:2] == key]:
            _gated_calls.pop(k, None)


def on_session_finalize(**kw):
    """Real session teardown: drop all per-session state."""
    sid = _sid(kw)
    with _lock:
        _delegation_ok.discard(sid)
        for k in [k for k in _failures if k[0] == sid]:
            _failures.pop(k, None)
        for k in [k for k in _pending_writes if k[0] == sid]:
            _pending_writes.pop(k, None)
        _turn_grants.pop(sid, None)
        for k in [k for k in _gated_calls if k[0] == sid]:
            _gated_calls.pop(k, None)
        for k in [k for k in _mandate_events if k[0] == sid]:
            _mandate_events.pop(k, None)
    if _settings()["features"].get("checkout_lease", True):
        _release_leases(sid, _settings())


def register(ctx):
    global _ctx
    _ctx = ctx  # settings surface; hooks stay registered even when the master switch is off,
    # so a config flip (enabled/log_only/features) takes effect without a gateway restart
    ctx.register_hook("pre_llm_call", pre_llm_call)
    ctx.register_hook("pre_tool_call", pre_tool_call)
    ctx.register_hook("post_tool_call", post_tool_call)
    ctx.register_hook("on_session_end", on_session_end)
    ctx.register_hook("on_session_finalize", on_session_finalize)

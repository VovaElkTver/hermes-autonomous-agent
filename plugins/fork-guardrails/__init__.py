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

v0.4.0 (23.11, mech_proposals M2/M4/M6 after incident dgx98): remote-write gate
against an owner-maintained host allowlist (reads never gate; a write over
ssh/scp/rsync/sftp to a host absent from the list blocks), the allowlist itself
is owner property (agent writes to it block too), and every gated remote write
leaves a line in write_ledger.jsonl (host, targets, decision, best-effort md5
before — md5 after lands asynchronously from a daemon thread on success).
M4's child-side half lives in tools/delegate_tool_progress.py: every child
system prompt carries a <delegation-scope> block naming the same allowlist; the
child cannot extend it, and enforcement is this gate, not the prompt.
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
             "remote_write_gate", "write_ledger")
DEFAULT_AUDIT_LOG = "~/.hermes/fork-guardrails/audit.jsonl"


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


# ── v0.4.0 M2: remote-write gate + owner allowlist; M6: write ledger ───────
ALLOWLIST_FILENAME = "host-allowlist"
SSH_OPTS_WITH_VALUE = {"-p", "-i", "-F", "-o", "-l", "-c", "-b", "-D", "-J", "-L",
                       "-R", "-W", "-m", "-Q", "-w", "-E", "-e"}
WRITE_PATH_TOOLS = {"write_file", "patch"}
# transport verb, not a prefix of a bigger word (ssh-keygen, rsync:// stay out)
_TRANSPORT_RE = re.compile(r"\b(?:ssh|scp|rsync|sftp)\b(?![-\w])")
# write-class commands run remotely; '> ' redirect counts (bare '2>' stderr does not)
_WRITE_VERB_RE = re.compile(
    r"\b(?:rm|mv|cp|tee|install|mkdir|rmdir|touch|truncate|chmod|chown|dd|kill|pkill|killall"
    r"|usermod|userdel|crontab|systemctl\s+(?:stop|start|restart|enable|disable|mask|unmask|reload|daemon-)"
    r"|apt(?:-get)?\s+(?:install|remove|purge|autoremove)|docker\s+(?:run|compose\s+up))\b"
    r"|(?<![0-9&])>>?(?![=&])")
_ALLOWLIST_TOUCH_RE = re.compile(r"\b(?:tee|cp|mv|rm|install|truncate|touch)\b|>>|(?<![0-9&])>>(?![=&])|>\s*\S*host-allowlist|\bsed\s+-i")
_REMOTE_EP_RE = re.compile(r"^(?:[\w.+-]+@)?(\[[0-9A-Fa-f:.]+\]|(?![A-Za-z]:)[\w.-]+):")
_ABS_PATH_RE = re.compile(r"(?<![\w~$@])/[\w@.+-]+(?:/[\w@.+-]+)*")

_pending_writes: dict[tuple[str, str, str], dict] = {}


def _plugin_dir(s: dict) -> str:
    return os.path.dirname(os.path.expanduser(str(s.get("audit_log") or DEFAULT_AUDIT_LOG)))


def _allowlist_path(s: dict) -> str:
    return os.path.join(_plugin_dir(s), ALLOWLIST_FILENAME)


def _load_allowlist(path: str) -> set[str]:
    """Owner-maintained lines: host / user@host / IP, '#' comments. Empty/absent → nothing allowed."""
    entries: set[str] = set()
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                entry = line.split("#", 1)[0].strip().lower()
                if entry:
                    entries.add(entry)
    except OSError:
        pass
    return entries


def _host_allowed(token: str, entries: set[str]) -> bool:
    token = token.lower()
    host = token.rsplit("@", 1)[-1].strip("[]")
    return token in entries or host in entries


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


def _remote_write_check(text: str, tool_name: str, args: dict, kw: dict, s: dict, verdict_fn):
    """M2 gate + M6 preflight. Returns a verdict dict/None via verdict_fn (audit stays in one place)."""
    allow_path = _allowlist_path(s)
    path = str((args or {}).get("path") or "")
    if path and tool_name in WRITE_PATH_TOOLS and os.path.abspath(os.path.expanduser(path)) == os.path.abspath(allow_path):
        return verdict_fn({"action": "block", "rule_key": "fork-guardrails:allowlist_write",
                           "message": "host-allowlist is owner-maintained; agent writes to it are blocked."})
    if text and ALLOWLIST_FILENAME in text and _ALLOWLIST_TOUCH_RE.search(text):
        return verdict_fn({"action": "block", "rule_key": "fork-guardrails:allowlist_write",
                           "message": "host-allowlist is owner-maintained; agent writes to it are blocked."})
    hosts = _remote_write_hosts(text) if text else set()
    if not hosts:
        return None
    entries = _load_allowlist(allow_path)
    offenders = {h for h in hosts if not _host_allowed(h, entries)}
    targets = _remote_write_targets(text)
    single = (next(iter(hosts)), targets[0]) if (len(hosts) == 1 and len(targets) == 1 and "*" not in targets[0]) else None
    if offenders:
        verdict = verdict_fn({"action": "block", "rule_key": "fork-guardrails:remote_write",
                              "message": f"Remote write to {', '.join(sorted(offenders))} is not allowlisted; "
                                         "needs the owner's authorizing message for this host (reads are not gated)."})
        _ledger(s, {"kind": "remote_write", "decision": "block" if verdict or not s["log_only"] else "would_block",
                    "hosts": sorted(hosts), "targets": targets, "cmd": text[:300], "tool": tool_name,
                    "session_id": _sid(kw), "turn_id": kw.get("turn_id") or "",
                    "md5_before": None, "md5_note": "blocked"})
        return verdict
    md5, note = None, "log-only" if s["log_only"] else ("skipped: not a single exact target" if not single else None)
    if single and not s["log_only"]:
        md5 = _ssh_md5(*single)
        note = None if md5 else "read failed"
    _ledger(s, {"kind": "remote_write", "decision": "allow", "hosts": sorted(hosts), "targets": targets,
                "cmd": text[:300], "tool": tool_name, "session_id": _sid(kw), "turn_id": kw.get("turn_id") or "",
                "md5_before": md5, "md5_note": note})
    if s["features"].get("write_ledger", True):
        with _lock:
            _pending_writes[(_sid(kw), kw.get("turn_id") or "", _sig(tool_name, args or {}))] = {
                "hosts": sorted(hosts), "cmd": text[:300], "single": single if not s["log_only"] else None}
    return None


def _settle_pending_write(tool_name: str, args: dict, kw: dict, s: dict, status: str) -> None:
    """M6 post-fact line for a gated remote write; md5-after asynchronously when a single target is known."""
    key = (_sid(kw), kw.get("turn_id") or "", _sig(tool_name, args or {}))
    with _lock:
        ctxp = _pending_writes.pop(key, None)
    if ctxp is None or not s["features"].get("write_ledger", True):
        return
    _ledger(s, {"kind": "remote_write_result", "status": status, "hosts": ctxp["hosts"],
                "cmd": ctxp["cmd"], "tool": tool_name, "session_id": _sid(kw), "turn_id": kw.get("turn_id") or ""})
    if status == "ok" and ctxp.get("single"):
        host, target = ctxp["single"]

        def _after():
            _ledger(s, {"kind": "remote_md5_after", "host": host, "target": target, "md5": _ssh_md5(host, target)})
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
    if not s["features"]["date_context"]:
        return {}
    now = datetime.datetime.now().astimezone()
    return {"context": f"Current date: {now:%Y-%m-%d %H:%M %Z}. Verify post-cutoff facts with tools."}


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
    if s["features"]["remote_write_gate"] and (not text) and tool_name in WRITE_PATH_TOOLS:
        # write_file/patch carry no shell text: only the allowlist self-guard applies to them (M2)
        verdict = _remote_write_check(text, tool_name, args, kw, s, _verdict)
        if verdict is not None:
            return verdict
    if text:
        if s["features"]["force_push_block"] and FORCE_PUSH_PROTECTED.search(text):
            return _verdict({"action": "block",
                            "message": "Force-push to main/master is forbidden in this fork."})
        if s["features"]["remote_write_gate"]:
            verdict = _remote_write_check(text, tool_name, args, kw, s, _verdict)
            if verdict is not None:
                return verdict
        if s["features"]["outbound_approve"]:
            for key, pat in OUTBOUND.items():
                if pat.search(text):
                    return _verdict({"action": "approve", "message": f"{tool_name}: {text[:300]}",
                                     "rule_key": f"fork-guardrails:{key}"})
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
    if status in ("ok", "error") and s["features"]["remote_write_gate"]:
        _settle_pending_write(tool_name, args if isinstance(args, dict) else {}, kw, s, status)


def on_session_end(**kw):
    """Fires after EVERY turn (despite the name): drop that turn's retry counters."""
    key = (_sid(kw), kw.get("turn_id") or "")
    with _lock:
        _failures.pop(key, None)
        for k in [k for k in _pending_writes if k[:2] == key]:
            _pending_writes.pop(k, None)


def on_session_finalize(**kw):
    """Real session teardown: drop all per-session state."""
    sid = _sid(kw)
    with _lock:
        _delegation_ok.discard(sid)
        for k in [k for k in _failures if k[0] == sid]:
            _failures.pop(k, None)
        for k in [k for k in _pending_writes if k[0] == sid]:
            _pending_writes.pop(k, None)


def register(ctx):
    global _ctx
    _ctx = ctx  # settings surface; hooks stay registered even when the master switch is off,
    # so a config flip (enabled/log_only/features) takes effect without a gateway restart
    ctx.register_hook("pre_llm_call", pre_llm_call)
    ctx.register_hook("pre_tool_call", pre_tool_call)
    ctx.register_hook("post_tool_call", post_tool_call)
    ctx.register_hook("on_session_end", on_session_end)
    ctx.register_hook("on_session_finalize", on_session_finalize)

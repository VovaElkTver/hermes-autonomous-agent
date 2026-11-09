"""config-protection: mechanical gate against "agent under pressure edits the
guard-config instead of fixing the code" (ported from affaan-m/ECC hook of the
same name; prior-art: docs/design/critiques/ecc-agent-harness.md, Вл 10.11).

Design (ТЗ 10.11):
- block ONLY edits to EXISTING protected files; create-new and reads pass;
- protected list = config key ``protected_files`` (list of globs), re-read on
  every invocation (no restart). Fallback when the key is absent/invalid: the
  three guard configs named in the TZ;
- lstat (not exists) so a dangling symlink counts as existing;
- basename comparison case-insensitive;
- fail-open: any internal error never breaks the tool call, but each swallowed
  error prints one diagnostic line to stderr (never silently).
"""
from __future__ import annotations

import fnmatch
import os
import sys

log = __import__("logging").getLogger(__name__)

# Tools whose args carry a direct file path.
_PATH_ARG_TOOLS = {"patch", "write_file", "edit", "apply_patch", "str_replace_editor"}
# Free-text tools: scan text for path tokens hitting a protected glob.
_TEXT_TOOLS = {"terminal", "execute_code"}

FALLBACK_PROTECTED = [
    "~/.hermes*/config.yaml",
    "~/.hermes*/topics.yaml",
    "~/.hermes*/cron/jobs.json",
]


def _diag(msg: str) -> None:
    """One line per swallowed error — fail-open must stay observable (ТЗ п.1)."""
    print(f"[config-protection] {msg}", file=sys.stderr)


def _protected_globs() -> list[str]:
    try:
        from hermes_cli.config import load_config
        raw = load_config().get("protected_files")
        if isinstance(raw, list) and all(isinstance(x, str) for x in raw):
            return [os.path.expanduser(x) for x in raw]
        if raw is not None:
            _diag("config key 'protected_files' is not a list of strings; using fallback")
    except Exception as exc:  # config unreadable → fallback list, still diagnose
        _diag(f"config read failed ({exc}); using fallback list")
    return [os.path.expanduser(x) for x in FALLBACK_PROTECTED]


def _existing(path_str: str) -> bool:
    """lstat-based existence: dangling symlinks count as existing (ECC п.3)."""
    try:
        os.lstat(path_str)
        return True
    except OSError:
        return False


def _hit(path_str: str, globs: list[str]) -> bool:
    try:
        candidate = os.path.abspath(os.path.expanduser(path_str)).lower()
    except Exception as exc:
        _diag(f"path normalize failed for {path_str!r} ({exc})")
        return False
    return any(fnmatch.fnmatch(candidate, g.lower()) for g in globs)


def _candidates(tool_name: str, args: dict, basenames: frozenset[str]):
    if tool_name in _PATH_ARG_TOOLS:
        p = args.get("path") or args.get("file_path") or args.get("file")
        if p:
            yield str(p)
    elif tool_name in _TEXT_TOOLS:
        text = str(args.get("command") or args.get("code") or "")
        for tok in text.replace('"', " ").replace("'", " ").split():
            tok = tok.rstrip(",;)")
            if "/" in tok or tok.lower().split(os.sep)[-1].split("/")[-1] in basenames:
                yield tok


def pre_tool_call(tool_name: str = "", args: dict | None = None, **kw):
    try:
        args = args if isinstance(args, dict) else {}
        globs = _protected_globs()
        # basename set for quick token screening in free-text tools;
        # full glob matching below remains the authority
        basenames = frozenset(
            g.rsplit(os.sep, 1)[-1].rsplit("/", 1)[-1] for g in globs)
        for cand in _candidates(tool_name, args, basenames):
            try:
                if not _hit(cand, globs):
                    continue
                # existing-only: create-new passes; lstat catches dangling symlinks
                if not _existing(os.path.expanduser(cand)):
                    continue
                return {
                    "action": "block",
                    "message": f"config-protection: {cand} is a protected guard-config "
                               "(edits blocked by policy). Fix code instead, or ask the user.",
                }
            except Exception as exc:
                _diag(f"candidate check failed for {cand!r} ({exc})")
    except Exception as exc:
        _diag(f"pre_tool_call swallowed an unexpected error ({exc})")
    return None  # allow / no opinion


def register(ctx):
    ctx.register_hook("pre_tool_call", pre_tool_call)

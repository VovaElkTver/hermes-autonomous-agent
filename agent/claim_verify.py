"""Turn-end claim-integrity gate (docs/design/claim-verify.md doctrine 16.11).

Policy-only, like ``agent.verification_stop``: this module never executes
anything. At the stop point it inspects the candidate answer and the turn
history already present in ``messages``, and turns two corpus-proven failure
classes into ONE bounded follow-up when the model tries to finish:

  A. phantom path — the answer asserts a concrete file/dir as created/known
     (no explicit negation around it), the path does not exist on disk, and no
     successful file mutation this turn covers it. (Corpus case: the agent
     asserted an existing ``cron-jobs.json`` that was never there.)
  B. claim-without-effect — a completed external-effect verb (merged/pushed/
     restarted, RU+EN) with no matching effecting command anywhere in the
     current turn. (Corpus cluster: false_success ×11 — "смержено/готово"
     stated from stale evidence.)

Deliberate limits (honest, see tests): B matches a closed verb set and accepts
ANY same-family command in the turn as evidence (read-back quality is the
model's job — the nudge asks for it); A ignores paths whose parent does not
resolve (URL/API shapes stay out). Fail-open everywhere: a broken probe must
never break the turn that produced the real result.

Enabled by default; disable with ``HERMES_CLAIM_VERIFY=0`` or
``agent.claim_verify: false``. Budget: one nudge per turn, session-capped.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger("agent.claim_verify")

_FALSY_TOKENS = {"0", "false", "no", "off"}

_MAX_PATHS_IN_NUDGE = 5
_MAX_SESSION_NUDGES = 3

# Path-like tokens: ~/ ./ ../ /abs — must end on a word char, no trailing punct.
_PATH_TOKEN = re.compile(r"(?<![\w/~.])(~|\.{1,2})?/[A-Za-z0-9_.\-][A-Za-z0-9_.\-/]*[A-Za-z0-9_\-]")
# Explicit absence/negation language around the path (sentence window).
_NEG_RE = re.compile(
    r"(\bне\s+найд|\bне\s+существ|\bотсутств|\bнет\s+(?:файла|записи|таког|его|её)|"
    r"\bне\s+было|\bне\s+созда|\bне\s+пишет|\bудал|"
    r"\bmissing\b|\bdoesn'?t\s+exist|\bdoes\s+not\s+exist|\bnot\s+found\b|\bno\s+such\b|"
    r"\babsent\b|\bwas\s+deleted|\bwas\s+removed)",
    re.IGNORECASE,
)
_SENTENCE_SPLIT = re.compile(r"[.!?\n]+")

# Family B: completed-effect claims -> family -> (claim regex, evidence regex over
# tool-call payloads of the current turn).
_EFFECT_FAMILIES: list[tuple[str, re.Pattern[str], re.Pattern[str]]] = [
    (
        "git",
        re.compile(r"(смерж|смёрж|замерж|слит[аиео]?|влит[аиео]?|запуш|merged\b|pushed\b|смержено)", re.IGNORECASE),
        re.compile(r"\bgit\s+(push|merge|rebase|cherry-pick|reset|worktree|tag)\b", re.IGNORECASE),
    ),
    (
        "service",
        re.compile(r"(перезапуск[аио]?[лн]|перезапущ|рестартанул|restart(ed| initiated)?\b|ре-?start\b)", re.IGNORECASE),
        re.compile(r"systemctl(\s+--(?:user|system))?\s+(restart|start|stop|reload)|service\s+\S+\s+(restart|reload)", re.IGNORECASE),
    ),
]


def claim_verify_enabled(config: dict[str, Any] | None = None) -> bool:
    """``agent.claim_verify`` config (bool or falsy token); default ON for this fork."""
    if config is None:
        try:
            from hermes_cli.config import load_config_readonly

            config = load_config_readonly()
        except Exception:  # deliberate boundary — a broken config read must not disable honesty
            logger.debug("claim-verify config load failed, defaulting on", exc_info=True)
            config = {}
    agent_cfg = (config or {}).get("agent") or {}
    value = agent_cfg.get("claim_verify", True)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in _FALSY_TOKENS


def _turn_start_index(messages: list[dict[str, Any]]) -> int:
    """Current turn = everything after the last real (non-synthetic) user row."""
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if m.get("role") != "user":
            continue
        if any(k.startswith("_") and v for k, v in m.items()):
            continue
        return i + 1
    return 0


def _tool_payloads(messages: list[dict[str, Any]], start: int) -> str:
    parts: list[str] = []
    for m in messages[start:]:
        for call in m.get("tool_calls") or []:
            fn = call.get("function") if isinstance(call, dict) else None
            if isinstance(fn, dict):
                parts.append(str(fn.get("name") or ""))
                parts.append(str(fn.get("arguments") or ""))
    return "\n".join(parts)


def _mutation_names(agent: Any) -> set[str]:
    paths = getattr(agent, "_turn_file_mutation_paths", None) or set()
    names: set[str] = set()
    for raw in paths:
        try:
            names.add(str(Path(str(raw)).name))
            names.add(str(raw))
        except (TypeError, ValueError):  # deliberate boundary — junk entries skip
            continue
    return names


def _phantom_paths(text: str, agent: Any) -> list[str]:
    """Concrete absolute/~/relative paths asserted without evidence of existence."""
    mutated = _mutation_names(agent)
    found: list[str] = []
    for match in _PATH_TOKEN.finditer(text):
        token = match.group(0)
        before = text[max(0, match.start() - 8):match.start()]
        if before.endswith(("http:", "https:", ":")):
            continue
        core = token[1:] if token.startswith("/") else token.lstrip("./~")
        if core and "/" not in core and "." not in core:
            continue  # slash-command names (/refine, /fork) are not file paths
        sentence_start = max(
            (t.end() for t in _SENTENCE_SPLIT.finditer(text, 0, match.start())), default=0)
        sentence_end = min(
            (t.start() for t in _SENTENCE_SPLIT.finditer(text, match.end())), default=len(text))
        if _NEG_RE.search(text[sentence_start:sentence_end]):
            continue
        try:
            path = Path(token).expanduser()
        except (OSError, ValueError):
            continue
        try:
            if path.exists():
                continue
            parent_exists = path.parent.is_dir()
        except OSError:
            continue
        if not parent_exists:
            continue  # URL/API shapes (/v1/models) never resolve — out of scope
        if path.name in mutated or token in mutated:
            continue
        found.append(token)
        if len(found) >= _MAX_PATHS_IN_NUDGE:
            break
    return found


def _unsupported_claims(text: str, evidence_blob: str) -> list[str]:
    hits: list[str] = []
    for family, claim_re, evidence_re in _EFFECT_FAMILIES:
        if claim_re.search(text) and not evidence_re.search(evidence_blob):
            hits.append(family)
    return hits


def build_claim_nudge(agent: Any, final_response: Any, messages: list[dict[str, Any]]) -> str | None:
    """Nudge text when the answer carries unsupported claims; None when clean or
    disabled or budget spent. Never raises."""
    try:
        if not isinstance(final_response, str) or len(final_response) < 40:
            return None
        if not claim_verify_enabled():
            return None
        if int(getattr(agent, "_claim_verify_nudges", 0) or 0) >= _MAX_SESSION_NUDGES:
            return None
        start = _turn_start_index(messages)
        evidence = _tool_payloads(messages, start)
        phantoms = _phantom_paths(final_response, agent)
        families = _unsupported_claims(final_response, evidence)
        if not phantoms and not families:
            return None
        lines: list[str] = []
        if phantoms:
            lines.append(
                "фантомные пути (нет на диске, в этом ходе нет успешной записи): "
                + ", ".join(phantoms))
        if families:
            lines.append(
                "утверждения о завершённых внешних эффектах без исполняющей команды в этом ходе: "
                + ", ".join(families))
        return (
            "[claim-verify] Финальный ответ содержит непроверенные утверждения — "
            + "; ".join(lines)
            + ". Сделай read-back (test -e / git ls-remote / systemctl is-active / чтение "
            "изменённого состояния), поправь формулировки по факту или сними утверждения. "
            "Напоминание однократное за ход; статус понижается только тобой, не гейтом."
        )
    except Exception:  # deliberate boundary — claim probe must never break the turn
        logger.debug("claim-verify probe skipped", exc_info=True)
        return None

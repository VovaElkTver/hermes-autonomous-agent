"""Planning router for ``planning.mode=auto`` (delivery 3, P1a — code-only classifier).

Decision owner is CODE (Вл 14.11): a cheap deterministic pass splits turns into
``direct`` (no pipeline; observable skip) vs ``plan`` (full five-pass conveyor).
No LLM call here — latency budget for this stage is zero; the LLM advisory voice
for the residual grey zone is P1b and will only ever NARROW ``plan``, never flip
a deterministic verdict.

Bias policy (lesson 14.11: heuristic triggers go lazy -> systematic under-trigger):
the grey zone DEFAULTS TO PLAN. Only chat noise, status questions and pure
consultation slip past. False planning costs <= budget_seconds; a missed plan
costs the whole point of the feature.
"""

from __future__ import annotations

import re

# --- deterministic signals -------------------------------------------------
# Filesystem-ish reference: "tests/foo.py", "~/.hermes/config.yaml", "/home/..."
_PATH_RE = re.compile(
    r"(?:[\w.~+-]+/)+[\w.-]+|"          # anything with a directory slash
    r"[\w.-]+\.(?:py|md|json|ya?ml|toml|sh|ts|tsx|js|db|sqlite|log|txt)\b"
)

# Russian/English imperative stems that signal *work* is requested.
_IMPERATIVE_RE = re.compile(
    r"\b(?:сдела|доба|убер|удал|исправ|поправ|перепиш|реализу|почин"
    r"|напиши|собери|смерж|мерж|запуш|заккоммить|коммить"
    r"|внедр|включ|выключ|переключ|настро|проверя|провер|протести|тест"
    r"|оптимизиру|ускор|отрефактори|рефакторинг|мигриру|переезжа|подним"
    r"|разверн|обнов|upgrade|install|fix|add|remov|refactor|implement|deploy"
    r")\w*", re.IGNORECASE,
)

_EXPLICIT_PLAN_RE = re.compile(
    r"\b(?:составь|построй|сформируй|запланируй)\s+(?:нам\s+)?план\b"
    r"|\bплан:\s", re.IGNORECASE,
)

_MULTISTEP_MARKERS = ("потом", "затем", "после этого", "во-первых", "шаг",
                      "этап", "конвейер", "и потом")

_CHATTER_RE = re.compile(
    r"^(?:ок|окей|хорошо|спасибо|благодар|привет|здравствуй|ага|угу|да\b|нет\b"
    r"|понят|ясно|принял|\+|\?+)",
    re.IGNORECASE,
)

_FILE_HANDOVER_RE = re.compile(
    r"\b(?:кину|кидаю|пришлю|отправлю|лови)\b|\b(?:файл|документ|анализ|отчёт)\b",
    re.IGNORECASE,
)


def _is_file_handover(low: str) -> bool:
    """Verb and noun in ANY order ('я сейчас файл кину' / 'лови отчёт'), short msg."""
    if len(low) > 120:
        return False
    has_verb = re.search(r"\b(?:кину|кидаю|пришлю|отправлю|лови)\b", low)
    has_noun = re.search(r"\b(?:файл|документ|анализ|отчёт)\b", low)
    return bool(has_verb and has_noun)


def classify_route(text: str) -> tuple[str, str, float]:
    """Return ``(route, reason, confidence)`` where route is ``direct`` | ``plan``."""
    t = (text or "").strip()
    low = t.lower()
    if not t:
        return "direct", "empty_message", 1.0
    if _EXPLICIT_PLAN_RE.search(low):
        return "plan", "explicit_plan_request", 1.0

    paths = set(_PATH_RE.findall(t))
    imperative = bool(_IMPERATIVE_RE.search(low))

    # --- clearly non-trivial ------------------------------------------------
    if len(paths) >= 2:
        return "plan", f"multi_path({len(paths)})", 0.9
    if imperative and paths:
        return "plan", "imperative_with_path", 0.9
    if any(marker in low for marker in _MULTISTEP_MARKERS) and (imperative or paths):
        return "plan", "multistep_marker", 0.85
    if len(t) > 240:
        return "plan", "long_request", 0.75

    # --- clearly trivial ----------------------------------------------------
    if _CHATTER_RE.match(low) and not imperative and not paths:
        return "direct", "chatter_ack", 0.8
    if _is_file_handover(low):
        return "direct", "file_handover", 0.8
    if "?" in t and not imperative and not paths and len(t) < 120:
        return "direct", "status_question", 0.75
    if len(t) <= 12 and not imperative and not paths:
        return "direct", "very_short", 0.7

    # --- grey zone: bias to plan -------------------------------------------
    return "plan", "grey_zone_default", 0.5

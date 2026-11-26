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

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

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


# --- A1: grey-zone triage classifier (openhuman triage borrow) --------------
# Ported CONTRACT (not runtime): the four-action decision enum, the tolerant
# reply parser and the tiered cloud->local + Deferred semantics from
# vendor/openhuman .../triage/{README.md,decision.rs}. The cheap-model call is
# our own and injectable; flag OFF leaves the deterministic router untouched.

TRIAGE_ACTIONS: tuple[str, ...] = ("drop", "acknowledge", "react", "escalate")
TRIAGE_TO_ROUTE_SET = frozenset(TRIAGE_ACTIONS)

#: action -> deterministic route. drop/acknowledge need no pipeline; react/
#: escalate need work, so they plan.
_TRIAGE_TO_ROUTE: dict[str, str] = {
    "drop": "direct",
    "acknowledge": "direct",
    "react": "plan",
    "escalate": "plan",
}

_TRIAGE_CONFIDENCE: dict[str, float] = {
    "drop": 0.8,
    "acknowledge": 0.7,
    "react": 0.8,
    "escalate": 0.85,
}

#: classifier outcome tokens (the injected callable's return contract)
TRIAGE_DEFERRED = "defer"
TRIAGE_EXHAUSTED = "exhausted"

#: zero-tool classifier prompt (the JSON contract decision.rs parses).
TRIAGE_PROMPT_TEMPLATE = (
    "You are a triage classifier for an autonomous engineering agent. Decide "
    "what the agent should do about the inbound message. Do NOT act; decide.\n"
    "Pick exactly one action:\n"
    "- drop: noise, duplicate, spam, or irrelevant; no downstream work.\n"
    "- acknowledge: worth remembering but no agent action needed.\n"
    "- react: one narrow single-step side effect.\n"
    "- escalate: multi-step / needs planning or a considered reply.\n"
    "End your reply with ONE fenced JSON block, the last thing you emit:\n"
    '```json\n{{"action": "<drop|acknowledge|react|escalate>", "reason": "<one sentence>"}}\n```\n'
    "The parser is tolerant (fenced JSON, prose around it, trailing commas, "
    "wrong-case action) but the block must be the last JSON object.\n\n"
    "Inbound message:\n{message}"
)


@dataclass
class TriageDecision:
    """Parsed zero-tool triage reply (openhuman decision.rs contract)."""

    action: str                      # one of TRIAGE_ACTIONS
    reason: str = ""


def _last_json_object(text: str) -> str | None:
    """Last balanced top-level ``{...}`` object in ``text`` (None if none)."""
    depth = 0
    start: int | None = None
    last: str | None = None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start is not None:
                last = text[start:i + 1]
                start = None
    return last


def _repair_trailing_commas(obj: str) -> str:
    """Drop trailing commas before a closing brace/bracket (model artefact)."""
    return re.sub(r",\s*([}\]])", r"\1", obj)


def parse_triage_decision(raw: str | None) -> TriageDecision | None:
    """Tolerantly parse a zero-tool triage reply into a decision.

    Accepts a fenced ```json block, a bare JSON object embedded in prose,
    trailing commas and a wrong-case ``action``. Returns ``None`` when no
    valid decision is recoverable (caller then defers, never admits).
    """
    if not raw:
        return None
    text = raw.strip()
    candidates: list[str | None] = []
    candidates.extend(re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL))
    candidates.append(_last_json_object(text))
    for cand in candidates:
        if not cand:
            continue
        try:
            data = json.loads(_repair_trailing_commas(cand))
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        action = str(data.get("action", "")).strip().lower()
        if action in TRIAGE_TO_ROUTE_SET:
            return TriageDecision(action=action, reason=str(data.get("reason", "")).strip())
    return None


def _triage_prompt(message: str) -> str:
    return TRIAGE_PROMPT_TEMPLATE.format(message=(message or "").strip())


def build_triage_classifier(
    *,
    cloud_call: Optional[Callable[[str], str | None]] = None,
    local_call: Optional[Callable[[str], str | None]] = None,
    defer_budget: int = 3,
) -> Callable[[str], str]:
    """Assemble the tiered grey-zone triage call (openhuman triage §2).

    Cloud arm with ONE retry on a transient/429/malformed reply, then the
    local-model arm when configured. Budget exhaustion and all failures yield
    ``defer`` (retry later), or ``exhausted`` once the deferral budget is
    spent — never an admit-by-default. The returned ``f(text)`` returns
    ``'decided:<action>' | 'defer' | 'exhausted'``.
    """
    state = {"deferred": 0}

    def classify(message: str) -> str:
        prompt = _triage_prompt(message)
        # cloud arm: initial attempt + one retry on transient/malformed.
        for _attempt in (1, 2):
            raw: str | None
            try:
                raw = cloud_call(prompt) if cloud_call is not None else None
            except Exception:
                raw = None
            if raw:
                decision = parse_triage_decision(raw)
                if decision is not None:
                    return f"decided:{decision.action}"
            # malformed / transient -> the loop gives the single retry
        # local-model arm.
        if local_call is not None:
            try:
                raw = local_call(prompt)
            except Exception:
                raw = None
            if raw:
                decision = parse_triage_decision(raw)
                if decision is not None:
                    return f"decided:{decision.action}"
        # nothing decided: defer, or exhaust after defer_budget deferrals.
        state["deferred"] += 1
        if state["deferred"] > max(1, defer_budget):
            return TRIAGE_EXHAUSTED
        return TRIAGE_DEFERRED

    return classify


def default_cloud_call(prompt: str) -> str:
    """Zero-tool cheap-model completion (the cloud arm) via the aux router.

    Lazy-imported so module import stays light and OFF-path stays untouched.
    Returns the raw model text (the tolerant parser repairs its shape).
    """
    from agent.auxiliary_client import call_llm, extract_content_or_reasoning

    resp = call_llm(
        task="planning.triage",
        messages=[{"role": "user", "content": prompt}],
        tools=None,                       # zero tools: the classifier must only decide
        temperature=0.0,
        max_tokens=160,
        timeout=20.0,
    )
    return extract_content_or_reasoning(resp)


def triage_classifier_from_config(
    cfg: Optional[dict[str, Any]],
    *,
    local_call: Optional[Callable[[str], str | None]] = None,
    defer_budget: int = 3,
) -> Optional[Callable[[str], str]]:
    """Build the triage classifier when the planning.triage flag is ON.

    Returns ``None`` (flag OFF -> byte-identical deterministic router) unless
    ``cfg`` carries a truthy ``triage`` toggle.
    """
    if not cfg or not cfg.get("triage"):
        return None
    return build_triage_classifier(
        cloud_call=default_cloud_call,
        local_call=local_call,
        defer_budget=int(cfg.get("triage_defer_budget", defer_budget)),
    )


def schedule_triage_review(
    text: str,
    *,
    scope_task: str,
    period_s: int = 30,
    conn=None,
) -> Optional[str]:
    """Reference host policy for a Deferred triage: retry later via a review.

    Best-effort: registers a ``ctl_followup`` review scoped to ``scope_task``
    (which must be a real controller task) so the grey-zone decision is
    re-examined on the next review beat instead of parking. Returns the
    followup id, or ``None`` when the controller store is unavailable / the
    scope is not a live task (the deferral stays observable via the
    ``triage:deferred`` reason and journal). The controller import is lazy so
    the OFF path and module import stay light.
    """
    if not scope_task:
        return None
    try:
        from controller import followups, model

        own_conn = conn is None
        if own_conn:
            conn = model.connect()
        try:
            return followups.add(
                conn, kind="review", scope_task=scope_task,
                text=f"triage-deferred retry: {text[:120]}", period_s=period_s,
                created_by="planning-triage",
            )
        finally:
            if own_conn and conn is not None:
                conn.close()
    except Exception:
        return None


def classify_route(
    text: str,
    *,
    triage_classifier: Optional[Callable[[str], str]] = None,
    on_defer: Optional[Callable[[str], None]] = None,
) -> tuple[str, str, float]:
    """Return ``(route, reason, confidence)`` where route is ``direct`` | ``plan``.

    A1 (openhuman triage borrow): when ``triage_classifier`` is supplied the
    residual grey zone (after the deterministic rules, before the plan bias) is
    decided by a zero-tool cheap-model call whose reply maps to one of
    ``drop|acknowledge|react|escalate``. A classifier failure defers (retry
    later via ``on_defer``) rather than admitting by default; the deterministic
    ``grey_zone_default`` is the terminal state only after deferral is
    exhausted. With ``triage_classifier=None`` (flag OFF) the behaviour is
    byte-for-byte the prior deterministic router.
    """
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

    # --- grey zone: A1 triage classifier (flag-gated) ----------------------
    if triage_classifier is not None:
        outcome = "defer"
        try:
            outcome = triage_classifier(t) or "defer"
        except Exception:
            outcome = "defer"          # any classifier failure = deferred, never admit
        if outcome.startswith("decided:"):
            action = outcome.split(":", 1)[1]
            if action in _TRIAGE_TO_ROUTE:
                return (
                    _TRIAGE_TO_ROUTE[action],
                    f"triage:{action}",
                    _TRIAGE_CONFIDENCE.get(action, 0.7),
                )
            outcome = "defer"          # unrecognised action -> treat as failure
        if outcome == "exhausted":
            pass                        # deferral budget spent: fall through to default
        else:
            if on_defer is not None:
                on_defer(t)
            return "direct", "triage:deferred", 0.3

    # --- grey zone: bias to plan (also the terminal state of Deferred) -----
    return "plan", "grey_zone_default", 0.5

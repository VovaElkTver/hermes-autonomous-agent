"""L1 (TZ-memdoor-5090-v2): Kev decision contract — donor pkg/decision/decision.go
semantics (typed choice/score/boolean; NO fallback to a chat model; an
unavailable result is a FACT, never a negative answer; choice text <=255,
score <=10).

Ported as contract, not code (donor is Go; the fork fleet is Python).
"""

from __future__ import annotations

from dataclasses import dataclass

MAX_CHOICE_LEN = 255
MAX_SCORE = 10
LOCAL_PARALLEL = 1  # donor systemone/client.go:41-45 — one in-flight probe


@dataclass(frozen=True)
class Decision:
    kind: str                 # "choice" | "yesno" | "score"
    value: object | None      # option str | bool | float
    p: float | None           # posterior probability of `value`
    status: str               # "ok" | "unavailable"
    reason: str = ""          # why unavailable (never folded into a negative)

    @property
    def available(self) -> bool:
        return self.status == "ok"

    def __bool__(self) -> None:
        raise TypeError(
            "Decision has no truthiness — an unavailable verdict must not "
            "silently read as 'no'. Check .available / .value explicitly.")


def validate_choice(question: str, options) -> str | None:
    if not question or not str(question).strip():
        return "question required"
    if not isinstance(options, list) or len(options) < 2:
        return "choice requires >=2 options"
    for opt in options:
        if not isinstance(opt, str) or not opt.strip():
            return "options must be non-empty strings"
        if len(opt) > MAX_CHOICE_LEN:
            return f"option exceeds {MAX_CHOICE_LEN} chars"
    return None


def softmax_logprobs(logprobs: dict[str, float]) -> dict[str, float]:
    """Normalize {token: logprob} (already log-space) into probabilities."""
    import math
    if not logprobs:
        return {}
    top = max(logprobs.values())
    exps = {t: math.exp(lp - top) for t, lp in logprobs.items()}
    total = sum(exps.values())
    return {t: e / total for t, e in exps.items()}

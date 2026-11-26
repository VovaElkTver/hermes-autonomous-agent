"""S4 (TZ-memdoor-5090-v2): the deterministic half of the ask-gate ("разберись сам").

A question to the person is not pre-checked: the documented "act-don't-ask"
preference has no mechanic, so clarify can fire on something a plain ``ls``
answers. Borrow the donor's gate (gateway/ask_gate.go:20-70) without the model:
if the question is one the agent could answer itself — it names a path/URL/command
(a factual probe), carries no design/preference wording, and no read/exec has run
this turn yet — the clarify call is refused with an instruction to read/run/check
instead. A repeated identical question always passes ("an insisting model is not
walled off"). The model half (P>=0.8 on :8002) is the M-tier, not here.

OFF by default (``ask_gate.enabled=false`` / ``HERMES_ASK_GATE``): the gate is a
no-op and clarify behaves byte-for-byte as before.
"""

from __future__ import annotations

import os
import re

_ENV = "HERMES_ASK_GATE"
_OFF = {"0", "false", "no", "off"}

# "The agent could answer this itself" signal: the question names something that
# can be read, queried, or run — rather than a design/preference choice.
_PATH_RX = re.compile(
    r"(?:~/?[\w./-]|\.\.?/|\b/[\w.-]+|/home/|C:\\|\\?\s*/[\w.-]*\.(?:py|md|json|yaml|yml|txt|sh|log|conf|ini|toml))",
    re.IGNORECASE)
_URL_RX = re.compile(r"https?://", re.IGNORECASE)
_CMD_RX = re.compile(
    r"\b(?:command|cmd|shell|exec(?:ute)?|pytest|npm|pip|pipx|git|make|docker|curl|rsync|grep)\b",
    re.IGNORECASE)
# Words that mark a design/preference choice only the person can make → the gate
# must not refuse these (they are legitimately questions for the human).
_PREFERENCE = (
    "prefer", "pref", "colour", "color", "which", "would you like", "want to",
    "opinion", "design", "choice", "choose", "pick", "style", "recommend",
    "better", "decide", "call it", "цвет", "предпоч", "какой", "который",
    "выбор", "выбери", "мнение", "хочешь", "хочу", "дизайн", "стиль", "лучше",
    "реш", "рекоменд",
)

# read/exec family — a "self-check" the agent has already (or can) run.
_READ_EXEC_TOOLS = frozenset({
    "read_file", "search_files", "terminal", "execute_code", "read_resource",
    "web_search", "web_extract", "vision_analyze",
})

_config_cache: bool | None = None
_state: dict[str, dict] = {}
_current_turn: str = "default"


def _clear_cache() -> None:
    global _config_cache
    _config_cache = None


def ask_gate_enabled() -> bool:
    """``ask_gate.enabled`` (default **False**) / ``HERMES_ASK_GATE``."""
    global _config_cache
    env = os.environ.get(_ENV)
    if env is not None:
        return env.strip().lower() not in _OFF
    if _config_cache is not None:
        return _config_cache
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
    except Exception:
        _config_cache = False
        return False
    val = (cfg.get("ask_gate") or {}).get("enabled")
    _config_cache = bool(val) if val is not None else False
    return _config_cache


def _turn_state(turn_key: str) -> dict:
    st = _state.get(turn_key)
    if st is None:
        st = {"readexec": False, "asked": set()}
        _state[turn_key] = st
    return st


def set_current_turn(turn_id: str) -> None:
    """Turn start: a fresh turn resets its read/exec + asked state."""
    global _current_turn
    _current_turn = str(turn_id or "default")
    _state[_current_turn] = {"readexec": False, "asked": set()}


def current_turn_key() -> str:
    return _current_turn


def note_tool_call(tool_name: str, turn_key: str | None = None) -> None:
    """A tool just ran: mark the turn's read/exec state (no-op when OFF / unknown)."""
    if not ask_gate_enabled():
        return
    if str(tool_name) in _READ_EXEC_TOOLS:
        key = turn_key if turn_key is not None else _current_turn
        _turn_state(key)["readexec"] = True


def _has_factual_probe(text: str) -> bool:
    return bool(_PATH_RX.search(text) or _URL_RX.search(text) or _CMD_RX.search(text))


def _has_preference(text: str) -> bool:
    low = text.casefold()
    return any(w in low for w in _PREFERENCE)


def self_answerable(text: str, seen_read_exec: bool = False) -> bool:
    """Deterministic: factual probe, no preference wording, no read/exec yet."""
    if seen_read_exec:
        return False
    if _has_preference(text):
        return False
    return _has_factual_probe(text)


def _refuse_instruction(text: str) -> str:
    return (
        "Refused: this question looks answerable by you directly — read the "
        "named file/path, run the command, or check the result first, then "
        "re-ask only a design/preference choice or something only the person "
        f"knows. (S4 ask-gate; the exact question will be let through on a "
        f"repeat.) Question was: {text.strip()[:200]}"
    )


def gate(turn_key: str, question_text: str, seen_read_exec: bool | None = None) -> str | None:
    """Return a refusal instruction, or None to let the question through.

    A repeated identical question (same turn) always passes; a self-answerable
    one is refused once, with its hash recorded so the repeat is let through."""
    if not ask_gate_enabled():
        return None
    st = _turn_state(turn_key)
    norm = " ".join(str(question_text).split())
    key = norm.casefold()
    if key in st["asked"]:
        return None                       # an insisting model is not walled off
    readexec = bool(st["readexec"]) if seen_read_exec is None else bool(seen_read_exec)
    refusal: str | None = None
    if self_answerable(norm, seen_read_exec=readexec):
        refusal = _refuse_instruction(norm)
    st["asked"].add(key)
    return refusal

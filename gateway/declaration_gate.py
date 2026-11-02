"""Success-declaration gate: mark artifact-less "done" claims in final responses.

2026-11-03 incident contract: an append-merge fix was declared done ("строки
перенесены ровно один раз") while the critic verdict returned FAIL — a green test
run was presented as proof of correctness without stating what it did not cover.

The gate is a cheap deterministic post-filter over the FINAL outbound text: when
the response declares success but cites no verification artifact (test count,
commit hash, test/journal path, tool run), an explicit marker is appended. It
never blocks delivery, never rewrites meaning, never runs anything. Off-switch:
``HERMES_DECLARATION_GATE=0``.
"""

from __future__ import annotations

import os
import re

MARKER = "◌ Декларация без артефакта проверки [declaration-gate]"

# Success vocabulary seen in real incident replies (RU + common EN).
_DECLARATION_RE = re.compile(
    r"\b("
    r"сделан[аоы]|готов[аыо]|зел[ёе]н\w*|успешн\w*|исправлен\w*|перенесен\w*"
    r"|проверено живьём|закоммитил|деплой|задеплоен"
    r"|committed|deployed|all (?:green|passing)|fixed"
    r")\b",
    re.IGNORECASE,
)

# Artifacts that count as cited verification evidence.
_EVIDENCE_RE = re.compile(
    r"\b\d+\s*/\s*\d+\b"           # test counts: 13/13
    r"|\b[0-9a-f]{10,40}\b"        # git hashes (incl. 10-char shorts like d5810f700e)
    r"|\btests?/[\w./\-]+"         # tests/gateway/test_x.py
    r"|\bjournal/[\w.\-]+"
    r"|(?:/[\w.\-]+){2,}"          # absolute paths (/home/qwen/.../x.py)
    r"|\b(?:pytest|pyright|ruff|lint|sqlite3?)\b",
    re.IGNORECASE,
)

_MIN_LEN_TO_MARK = 16


def _enabled() -> bool:
    return os.environ.get("HERMES_DECLARATION_GATE", "1").strip().lower() not in {
        "0", "false", "off", "no",
    }


def annotate_unverified_declaration(response: str) -> str:
    """Append the unverified-claim marker when success is declared without an artifact."""
    if not response or len(response) < _MIN_LEN_TO_MARK or not _enabled():
        return response
    if _DECLARATION_RE.search(response) is None:
        return response
    if _EVIDENCE_RE.search(response) is not None:
        return response
    return f"{response}\n\n{MARKER}"

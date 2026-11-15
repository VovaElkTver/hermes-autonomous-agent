"""Threat scan for deferred prompts on the fork's own scheduled carriers.

Cron's excision (10.11) removed the scheduler, not the exposure: a deferred
prompt still runs unattended later, and the donor's runtime tripwire died with
``tools/cronjob_tools`` — leaving only an orphaned test. The surviving carriers
that store agent/user-authored text for later unattended execution are:

* ``task_queue`` (tasks_kernel v0) — queued task ``title``/``body`` and events;
* ``ctl_followup`` (controller.followups) — ``reminder``/``review`` ``text``.

Both chokepoints call :func:`scan_scheduled_text` at INSERT time (a re-armed
review row re-INSERTs nothing — ticker UPDATEs the existing row, so already
accepted text never faces a retroactive block).

Two threat surfaces, two scanner tiers, exactly as the donor had it: the small
user-authored text gets the strict pattern set; vetted skill bodies are
scanned at install time (``skills_guard``) and are NOT inlined into stored
deferred text, so the loose ``assembled`` tier has no carrier here and was not
ported. Invisible-unicode parity with the install-time scanner is a hard
invariant: this file imports the canonical set from ``threat_patterns`` rather
than keeping its own copy (donor lesson #35075 — a narrower local copy let an
obfuscated directive slip past one gate).
"""

from __future__ import annotations

import re

# Single source of truth shared with the install-time scanner (skills_guard): a
# narrower local copy once let obfuscated directives slip past this tripwire.
from tools.threat_patterns import INVISIBLE_CHARS as _SCHEDULED_INVISIBLE_CHARS

# Strict patterns — stored deferred text only. A directive-shaped task/followup
# prompt has no business containing `cat ~/.hermes/.env` or `rm -rf /`; there
# it is a smoking gun, not prose.
_THREAT_PATTERNS = [
    (r'ignore\s+(?:\w+\s+)*(?:previous|all|above|prior)\s+(?:\w+\s+)*instructions', "prompt_injection"),
    (r'do\s+not\s+tell\s+the\s+user', "deception_hide"),
    (r'system\s+prompt\s+override', "sys_prompt_override"),
    (r'disregard\s+(your|all|any)\s+(instructions|rules|guidelines)', "disregard_rules"),
    (r'cat\s+[^\n]*(\.env|credentials|\.netrc|\.pgpass|id_rsa|id_ed25519|id_ecdsa)', "read_secrets"),
    (r'authorized_keys', "ssh_backdoor"), (r'/etc/sudoers|visudo', "sudoers_mod"),
    (r'rm\s+-rf\s+/', "destructive_root_rm"),
]

_SECRET_VAR_RE = r'\$\{?\w*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)\w*\}?'
# Obvious leak paths only: secret in the destination URL, in a POST/form body,
# or in an Authorization header to an arbitrary host.
_EXFIL_COMMAND_PATTERNS = [
    (rf'curl\s+[^\n]*https?://[^\s"\'`]*{_SECRET_VAR_RE}', "exfil_curl_url"),
    (rf'wget\s+[^\n]*https?://[^\s"\'`]*{_SECRET_VAR_RE}', "exfil_wget_url"),
    (rf'curl\s+[^\n]*(?:--data(?:-raw|-binary|-urlencode)?|-d|--form|-F)\s+[^\n]*{_SECRET_VAR_RE}', "exfil_curl_data"),
    (rf'wget\s+[^\n]*--post-(?:data|file)=[^\n]*{_SECRET_VAR_RE}', "exfil_wget_post"),
    (rf'curl\s+[^\n]*(?:-H|--header)\s+["\']Authorization:\s*(?:Bearer|token)\s+{_SECRET_VAR_RE}["\']', "exfil_curl_auth_header"),
]

# U+200D (ZWJ) is a required part of many emoji sequences (👨‍👩‍👧, 🏳️‍🌈): block it
# between plain text, allow it inside an emoji grapheme cluster.
_EMOJI_NEIGHBOUR_CP_RANGES = ((0x1F000, 0x1FFFF), (0x2600, 0x27BF), (0x2300, 0x23FF), (0x1F1E6, 0x1F1FF), (0x20E3, 0x20E3))
_VARIATION_SELECTOR_CP = 0xFE0F


def _zwj_has_emoji_neighbour(text: str, idx: int) -> bool:
    """True when the ZWJ at text[idx] sits between emoji codepoints (skipping VS16)."""
    left = idx - 1
    while left >= 0 and ord(text[left]) == _VARIATION_SELECTOR_CP:
        left -= 1
    right = idx + 1
    while right < len(text) and ord(text[right]) == _VARIATION_SELECTOR_CP:
        right += 1
    if left < 0 or right >= len(text):
        return False
    return all(
        any(lo <= ord(text[pos]) <= hi for lo, hi in _EMOJI_NEIGHBOUR_CP_RANGES) for pos in (left, right)
    )


def _strip_safe_constructs(text: str) -> str:
    """Scrub the bundled GitHub skill's `Authorization: token $GITHUB_TOKEN` +
    api.github.com curl so it doesn't trip the auth-header exfil rule.

    re.sub scrubs EVERY occurrence. The trailing ``[^\\s;&|$`]*`` consumes only
    the URL path — never separators or subshell openers — so a payload
    smuggled onto the same line still gets scanned. Host must be exactly
    api.github.com followed by ``/``, whitespace, quote, or end: lookalike
    authorities (api.github.com.evil.com, api.github.com@evil.com) fall through.
    """
    return re.sub(
        rf'curl\s+[^\n;&|$`]*(?:-H|--header)\s+["\']Authorization:\s*(?:Bearer|token)\s+{_SECRET_VAR_RE}["\']'
        r'\s+["\']?https://api\.github\.com(?::\d+)?(?:/|\s|$|["\'])[^\s;&|$`]*',
        'curl https://api.github.com/user',
        text,
        flags=re.IGNORECASE,
    )


def _strip_invisible_unicode(text: str) -> tuple[str, list[str]]:
    """Strip invisible-unicode chars, keeping ZWJ inside legitimate emoji.

    Returns ``(cleaned, sorted U+XXXX labels removed)``.
    """
    if not text:
        return text, []
    removed: set[str] = set()
    cleaned: list[str] = []
    for idx, ch in enumerate(text):
        if ch in _SCHEDULED_INVISIBLE_CHARS and not (ch == '\u200d' and _zwj_has_emoji_neighbour(text, idx)):
            removed.add(f"U+{ord(ch):04X}")
            continue
        cleaned.append(ch)
    return ''.join(cleaned), sorted(removed)


def _first_pattern_error(text: str) -> str:
    for patterns in (_THREAT_PATTERNS, _EXFIL_COMMAND_PATTERNS):
        for pattern, pid in patterns:
            if re.search(pattern, text, re.IGNORECASE):
                return (
                    f"Blocked: deferred text matches threat pattern '{pid}'. Queued task and "
                    "follow-up text must not contain injection or exfiltration payloads."
                )
    return ""


def scan_scheduled_text(text: str) -> str:
    """Strict scan of one stored deferred prompt (task item text, follow-up text).

    Returns an error string when blocked, else "". Invisible unicode is
    reported first, in ``_SCHEDULED_INVISIBLE_CHARS`` order (emoji ZWJ allowed).
    """
    to_scan = _strip_safe_constructs(text)
    removed = set(_strip_invisible_unicode(to_scan)[1])
    for char in _SCHEDULED_INVISIBLE_CHARS:
        if f"U+{ord(char):04X}" in removed:
            return f"Blocked: deferred text contains invisible unicode U+{ord(char):04X} (possible injection)."
    return _first_pattern_error(to_scan)

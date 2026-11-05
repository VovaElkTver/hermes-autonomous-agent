"""``/for`` — force a set through the controller queue, one item per step.

Grammar (set-builder form)::

    /for <A> in <set> [instruction... [--A...]] [--limit N] [--go]
    /for <set> [instruction...] [--limit N] [--go]      # legacy, still parses

``<A>`` is any unicode identifier (e.g. ``file``, ``файл``, ``card``); the
instruction may mention it as ``<A>`` and every step title binds it::

    /for card in ~/corpus/*.md переписать <card> по шаблону

``<set>`` is a *designation* of a collection, one of:

- ``{a, b, c}``  — literal set of items (any strings: ids, urls, names);
- ``@list.txt``  — file of items, one per line (``#`` comments and blanks skipped);
- ``path/to/dir`` or a ``glob``  — the catalog forms: a directory's top-level
  files (dotfiles skipped), glob file matches, or a single file itself.

Every item becomes one ``ctl_step`` of a single draft controller task, and the
task's workdir gets an autonomy permission granted via ``user-command``. The
pump then drives the items strictly in order — no batch script, no agent-chosen
loop. Dispatch waits for the human ``иди`` (draft tasks are never self-starting).
"""
from __future__ import annotations

import glob as globmod
import os
import re
from dataclasses import dataclass, field

DEFAULT_LIMIT = 200
HARD_MAX = 1000

ENFORCEMENT_NOTE = (
    "Work STRICTLY item-by-item in step order: read the step title, do exactly "
    "that item with tools, close the step, take the next pending one. Batch "
    "scripts that walk the set themselves are forbidden — the queue is "
    "the loop."
)

# A binding name: starts with a letter (any script), then word chars/dots/dashes.
# The "in" keyword requirement disambiguates from positional paths.
_VAR_RE = re.compile(r"[^\W\d_][\w.-]*", re.UNICODE)


@dataclass
class ForPlan:
    workdir: str
    goal: str
    var: str | None = None
    acceptance: list[str] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)  # [{"kind":..., "title":...}]
    truncated: int = 0


def _strip_flags(tokens: list[str]) -> tuple[list[str], int, bool]:
    limit = DEFAULT_LIMIT
    if "--limit" in tokens:
        i = tokens.index("--limit")
        try:
            limit = int(tokens[i + 1])
        except (IndexError, ValueError) as exc:
            raise ValueError("--limit needs an integer") from exc
        del tokens[i:i + 2]
        if limit < 1 or limit > HARD_MAX:
            raise ValueError(f"--limit must be 1..{HARD_MAX}")
    go = False
    if "--go" in tokens:
        go = True
        tokens.remove("--go")
    return tokens, limit, go


def parse_for_args(arg: str) -> dict:
    """Split the command line into {var, path, instruction, limit, go}.

    ``var`` is None for the legacy positional form. ``path`` is the raw set
    designation (literal ``{...}`` braces included; whitespace inside braces
    is preserved by regrouping until the braces balance).
    """
    tokens = arg.strip().split()
    if not tokens:
        raise ValueError("usage: /for <A> in <set> [instruction...] | "
                         "/for <path|glob> [instruction...] [--limit N] [--go]")
    tokens, limit, go = _strip_flags(list(tokens))
    if not tokens:
        raise ValueError("/for needs a set designation before any flags")

    if len(tokens) >= 2 and tokens[1] == "in" and _VAR_RE.fullmatch(tokens[0]):
        if len(tokens) < 3:
            raise ValueError("binding form needs a set designation after 'in'")
        var = tokens[0]
        rest = tokens[2:]
        if rest and rest[0].startswith("{"):
            joined, depth, consumed = [], 0, 0
            for tok in rest:
                joined.append(tok)
                depth += tok.count("{") - tok.count("}")
                consumed += 1
                if depth <= 0:
                    break
            spec = " ".join(joined)
            instruction = " ".join(rest[consumed:]).strip()
        else:
            spec, instruction = rest[0], " ".join(rest[1:]).strip()
        return {"var": var, "path": spec, "instruction": instruction,
                "limit": limit, "go": go}

    return {"var": None, "path": tokens[0],
            "instruction": " ".join(tokens[1:]).strip(),
            "limit": limit, "go": go}


def expand_items(path_expr: str, limit: int = DEFAULT_LIMIT) -> tuple[str, list[str], int]:
    """Return ``(workdir, entries, truncated_count)`` for catalog designations.

    A directory contributes its sorted top-level files; a glob pattern
    contributes its sorted file matches. Dotfiles are skipped. Raises
    ``ValueError`` with a chat-friendly message when nothing matches.
    """
    expanded = os.path.expanduser(path_expr)
    if any(ch in expanded for ch in "*?["):
        matches = [m for m in sorted(globmod.glob(expanded)) if os.path.isfile(m)]
        root = os.path.commonprefix([os.path.dirname(m) for m in matches]) if matches else ""
        workdir = os.path.abspath(root or ".")
    else:
        if not os.path.exists(expanded):
            raise ValueError(f"path does not exist: {path_expr}")
        if os.path.isdir(expanded):
            base = os.path.abspath(expanded)
            matches = sorted(
                os.path.join(expanded, name)
                for name in os.listdir(expanded)
                if os.path.isfile(os.path.join(expanded, name)) and not name.startswith("."))
        else:
            base = os.path.dirname(os.path.abspath(expanded))
            matches = [os.path.abspath(expanded)]
        workdir = base
    if not matches:
        raise ValueError(f"no files matched: {path_expr}")
    truncated = max(0, len(matches) - limit)
    return workdir, matches[:limit], truncated


def resolve_set(spec: str, limit: int = DEFAULT_LIMIT) -> tuple[str, list[str], int]:
    """Resolve a set designation into ``(workdir, item labels, truncated)``.

    Catalog items are labelled by basename (the workdir grant carries the
    directory context); literal/list items keep their exact text.
    """
    s = spec.strip()
    if s.startswith("{") and s.endswith("}"):
        members = [m.strip() for m in s[1:-1].split(",") if m.strip()]
        if not members:
            raise ValueError("empty literal set: write at least one item inside { }")
        truncated = max(0, len(members) - limit)
        return os.getcwd(), members[:limit], truncated
    if s.startswith("@"):
        list_path = os.path.expanduser(s[1:])
        if not os.path.isfile(list_path):
            raise ValueError(f"set file does not exist: {s}")
        with open(list_path, encoding="utf-8") as fh:
            members = [ln.strip() for ln in fh
                       if ln.strip() and not ln.lstrip().startswith("#")]
        if not members:
            raise ValueError(f"set file has no items: {s}")
        workdir = os.path.dirname(os.path.abspath(list_path))
        truncated = max(0, len(members) - limit)
        return workdir, members[:limit], truncated
    workdir, files, truncated = expand_items(s, limit)
    return workdir, [os.path.basename(f) for f in files], truncated


def build_plan(path_expr: str, instruction: str, limit: int = DEFAULT_LIMIT,
               var: str | None = None) -> ForPlan:
    """Itemize a set designation into a draft-queue plan (no DB writes here)."""
    workdir, items, truncated = resolve_set(path_expr, limit)
    what = instruction or "Process this item."
    designator = f"<{var}> in {path_expr}" if var else path_expr
    goal_lines = [
        f"/for {designator} — {len(items)} item(s), one step each.",
        f"Per-item instruction: {what}",
    ]
    if var:
        goal_lines.append(
            f"Binding: every step title names the item bound to <{var}> for "
            "that step; the instruction's mentions of "
            f"<{var}> resolve to exactly that item and nothing else."
        )
    goal_lines.append(ENFORCEMENT_NOTE)
    if var:
        steps = [{"kind": "impl", "title": f"{i}. {var} = {item}"}
                 for i, item in enumerate(items, start=1)]
    else:
        steps = [{"kind": "impl", "title": f"{i}. {item}"}
                 for i, item in enumerate(items, start=1)]
    acceptance = [
        "Each step names exactly one set item and is closed with output "
        "proving that item was processed.",
        "No item skipped silently: anything unprocessable ends blocked with a reason.",
    ]
    if truncated:
        goal_lines.append(f"(truncated: {truncated} more items beyond --limit {limit})")
    return ForPlan(workdir=workdir, goal="\n".join(goal_lines), var=var,
                   acceptance=acceptance, steps=steps, truncated=truncated)

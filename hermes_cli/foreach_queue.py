"""``/for`` — force a catalog through the controller queue, one item per step.

The user points the command at a directory or a glob; every entry becomes one
``ctl_step`` of a single draft controller task, and the task's workdir gets an
autonomy permission granted via ``user-command``. The pump then drives the
items strictly in order — no batch script, no agent-chosen loop. Dispatch waits
for the human ``иди`` (draft tasks are never self-starting).
"""
from __future__ import annotations

import glob as globmod
import os
from dataclasses import dataclass, field

DEFAULT_LIMIT = 200
HARD_MAX = 1000

ENFORCEMENT_NOTE = (
    "Work STRICTLY item-by-item in step order: read the step title, do exactly "
    "that item with tools, close the step, take the next pending one. Batch "
    "scripts that walk the directory themselves are forbidden — the queue is "
    "the loop."
)


@dataclass
class ForPlan:
    workdir: str
    goal: str
    acceptance: list[str] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)  # [{"kind":..., "title":...}]
    truncated: int = 0


def parse_for_args(arg: str) -> dict:
    """Split ``<path|glob> [instruction...] [--limit N]`` into its parts."""
    tokens = arg.strip().split()
    if not tokens:
        raise ValueError("usage: /for <path|glob> [instruction...] [--limit N]")
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
    path = tokens[0]
    instruction = " ".join(tokens[1:]).strip()
    return {"path": path, "instruction": instruction, "limit": limit}


def expand_items(path_expr: str, limit: int = DEFAULT_LIMIT) -> tuple[str, list[str], int]:
    """Return ``(workdir, entries, truncated_count)``.

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


def build_plan(path_expr: str, instruction: str, limit: int = DEFAULT_LIMIT) -> ForPlan:
    """Itemize *path_expr* into a draft-queue plan (no DB writes here)."""
    workdir, items, truncated = expand_items(path_expr, limit)
    what = instruction or "Process this item."
    goal_lines = [
        f"/for {path_expr} — {len(items)} item(s), one step each.",
        f"Per-item instruction: {what}",
        ENFORCEMENT_NOTE,
    ]
    steps = [{"kind": "impl", "title": f"{i}. {os.path.basename(p)}"}
             for i, p in enumerate(items, start=1)]
    acceptance = [
        "Each step names exactly one catalog item and is closed with output "
        "proving that item was processed.",
        "No item skipped silently: anything unprocessable ends blocked with a reason.",
    ]
    if truncated:
        goal_lines.append(f"(truncated: {truncated} more items beyond --limit {limit})")
    return ForPlan(workdir=workdir, goal="\n".join(goal_lines),
                   acceptance=acceptance, steps=steps, truncated=truncated)

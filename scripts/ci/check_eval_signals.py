#!/usr/bin/env python3
"""B5 (TZ-2026-11-16-dsh-B): eval signals never enter the model's input.

Doctrine (docs/design/eval-signals-contour.md): eval labels, the GEPA
corpus, and future review evaluations are WRITTEN into the eval contour
only; no prompt-assembly path reads them. This guard is the mechanical
half — a CI step, pure stdlib, run on the bare checkout:

* TEXT-level: the prompt-assembly surface group must not carry the
  eval-signal identifiers ``eval_label`` / ``gepa`` anywhere (a comment
  mentioning them is already a leak smell).
* AST-level: the identifier ``feedback`` must not appear in code
  positions (Name/Attribute/alias/def) of the surface group; prose in
  comments and docstrings stays out of scope — the word is ordinary
  English, the leak is the identifier.

The list is EXTENSIBLE: add new eval-signal names to _TEXT_FORBIDDEN /
_AST_FORBIDDEN below (or new surface files to PROMPT_ASSEMBLY_SURFACE)
when a new eval contour lands. The eval-writer itself (where the labels
are written) is whitelisted — it lives OUTSIDE the surface group; if a
writer module ever gets pulled into prompt assembly, it must be listed
in _EVAL_WRITER_WHITELIST and reviewed in CODEOWNERS first.

Exit codes: 0 = clean, 1 = violation found, 2 = inventory error
(a surface file is missing or unparseable: fail closed).
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

#: The prompt-assembly surface group (B5): the code that builds what the
#: model sees. Extensible — a new prompt surface joins here.
PROMPT_ASSEMBLY_SURFACE = (
    "agent/prompt_builder.py",
    "agent/skills_index_delta.py",
    "agent/prompt_caching.py",
    "agent/turn_context_compaction.py",
)

#: Text-level forbidden identifiers (case-insensitive, any occurrence
#: — including comments). Extensible.
_TEXT_FORBIDDEN = ("eval_label", "gepa")

#: AST-level forbidden identifiers (code positions only: Name,
#: Attribute, alias, def/class names; prose is out of scope). Extensible.
_AST_FORBIDDEN = ("feedback",)

#: The eval-writer side (where the labels are written) is whitelisted —
#: these files may carry the names; they are NOT prompt assembly.
_EVAL_WRITER_WHITELIST: tuple[str, ...] = ()


class _NameCollector(ast.NodeVisitor):
    def __init__(self, root: ast.AST) -> None:
        self.root = root
        self.hits: list[tuple[int, str]] = []

    def _walk(self, node: ast.AST) -> None:
        for name in self._names_of(node):
            if name in _AST_FORBIDDEN:
                line = getattr(node, "lineno", 0)
                self.hits.append((line, name))
        for child in ast.iter_child_nodes(node):
            self._walk(child)

    @staticmethod
    def _names_of(node: ast.AST) -> list[str]:
        out: list[str] = []
        if isinstance(node, ast.Name):
            out.append(node.id)
        elif isinstance(node, ast.Attribute):
            out.append(node.attr)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                out.append(alias.name)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            out.append(node.name)
        return out

    def visit(self, node: ast.AST) -> None:
        self._walk(node)


def _scan_text(path: Path, text: str) -> list[tuple[int, str]]:
    hits: list[tuple[int, str]] = []
    lowered = _TEXT_FORBIDDEN
    for lineno, line in enumerate(text.splitlines(), 1):
        low = line.lower()
        for token in lowered:
            if token in low:
                hits.append((lineno, token))
    return hits


def _scan_ast(path: Path, text: str) -> list[tuple[int, str]]:
    tree = ast.parse(text, filename=str(path))
    collector = _NameCollector(tree)
    collector.visit(tree)
    return collector.hits


def check(repo_root: str | Path) -> tuple[list[str], list[str]]:
    """Run the guard. Returns (violations, inventory_errors)."""
    root = Path(repo_root)
    violations: list[str] = []
    inventory_errors: list[str] = []
    for rel in PROMPT_ASSEMBLY_SURFACE:
        path = root / rel
        if not path.is_file():
            inventory_errors.append(f"surface file missing: {rel}")
            continue
        if rel in _EVAL_WRITER_WHITELIST:
            continue
        text = path.read_text(encoding="utf-8")
        try:
            text_hits = _scan_text(path, text)
            ast_hits = _scan_ast(path, text)
        except SyntaxError as exc:
            inventory_errors.append(f"unparseable surface file {rel}: {exc}")
            continue
        for lineno, token in text_hits:
            violations.append(f"{rel}:{lineno}: eval-signal identifier {token!r} in prompt assembly (B5)")
        for lineno, name in ast_hits:
            violations.append(f"{rel}:{lineno}: identifier {name!r} in prompt-assembly code position (B5)")
    return violations, inventory_errors


def main(argv: list[str]) -> int:
    repo_root = argv[1] if len(argv) > 1 else "."
    violations, inventory_errors = check(repo_root)
    for err in inventory_errors:
        print(f"::error::{err}")
    for v in violations:
        print(f"::error::{v}")
    if inventory_errors:
        print("eval-signal guard: FAILED (inventory error — fail closed)")
        return 2
    if violations:
        print(f"eval-signal guard: FAILED ({len(violations)} violation(s))")
        return 1
    print("eval-signal guard: clean "
          f"({len(PROMPT_ASSEMBLY_SURFACE)} surface files, "
          f"forbidden text {_TEXT_FORBIDDEN} / ast {_AST_FORBIDDEN})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

# Contour: eval signals never enter the model's input

Status: live (guard in CI since `elk/dsh-b`, TZ-2026-11-16-dsh-B §B5).
Owner: @hermes (rtx5090). Doctrine page; the mechanical half is
`scripts/ci/check_eval_signals.py` (CI step in `.github/workflows/repo-guards.yml`).

## The rule

Eval signals — eval labels, the GEPA corpus, and any future review
evaluation results — are **written into the eval contour only**. No
prompt-assembly path reads them. The model must never see its own
evaluation: a label or a scored review fed back into the prompt is a
self-reinforcing loop the controller has to see from outside.

Concretely:

* Labels/corpora land in the eval contour (the GEPA analysis lives
  outside the repo; in-repo eval writers will live in their own
  modules, never inside the prompt assembly).
* The prompt-assembly surface group (`agent/prompt_builder.py`,
  `agent/skills_index_delta.py`, `agent/prompt_caching.py`,
  `agent/turn_context_compaction.py`) carries no eval-signal
  identifiers. That is what the guard enforces.
* Derived artifacts (skills, need2write cards) are the documented
  channel by which *decisions* reach the prompt — they carry rules,
  not raw eval labels. A card that pastes a label or an incident log is
  the known bad shape (see the need2write doctrine), not this contour.

## The guard

`scripts/ci/check_eval_signals.py` (pure stdlib, bare-checkout,
fail-closed on missing/unparseable surface files):

* **text-level** in the surface group: `eval_label`, `gepa`
  (case-insensitive, any occurrence — a comment mentioning them is
  already a leak smell).
* **AST-level** in the surface group: the identifier `feedback` in code
  positions (Name/Attribute/import/def). Prose in comments and
  docstrings is out of scope — the word is ordinary English; the leak
  is the identifier.
* Exit codes: 0 clean, 1 violation, 2 inventory error.

## Extension procedure

* New eval-signal name → add it to `_TEXT_FORBIDDEN` / `_AST_FORBIDDEN`
  in the guard script (one line each) and the guard test.
* New prompt surface → add the file to `PROMPT_ASSEMBLY_SURFACE`.
* An eval-writer module that legitimately carries the names stays
  OUTSIDE the surface group; if one is ever pulled into prompt
  assembly, it must be named in `_EVAL_WRITER_WHITELIST` and reviewed
  in CODEOWNERS first.

## Audit (as of this commit)

Repo-wide `grep -ril` over tracked `*.py` (excluding venvs):

* `eval_label`: **0** occurrences repo-wide.
* `gepa`: **0** occurrences repo-wide (GEPA analysis lives in
  `~/projects/gepa`, not in the fork).
* `feedback`: 76 files carry the word in prose (comments, docstrings,
  docs); **0 identifier occurrences in the prompt-assembly surface
  group** (the AST scan is the check that matters here).

Nothing required migration: the forbidden names were never present in
the prompt assembly. The guard exists to keep it that way; old
occurrences elsewhere are out of scope by design (prose, not a code
path).

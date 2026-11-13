"""``/plan`` — build the plan-mode prompt: a saved markdown implementation plan, no execution.

A first-class built-in with no engine and no model-tool footprint: every surface
feeds :func:`build_plan_prompt` to the agent as a normal turn (like ``/learn``),
so system prompt and history stay untouched (prompt-cache safe).
"""

from __future__ import annotations

# Ground rules + authoring craft (writing-craft adapted from obra/superpowers).
_PLAN_MODE_RULES = """\
For this turn, you are in PLAN MODE — planning only.

- Do not implement code.
- Do not edit project files except the plan markdown file itself.
- Do not run mutating terminal commands, commit, push, or perform external
  actions.
- You may inspect the repo or other context with read-only commands/tools
  when needed.
- Your deliverable is a markdown plan saved inside the active workspace under
  `.hermes/plans/YYYY-MM-DD_HHMMSS-<slug>.md` (create the directory if
  needed; Hermes file tools are backend-aware, so this relative path keeps
  the plan with the workspace on local, docker, ssh, modal, and daytona
  backends). If the runtime provides a specific target path, use that exact
  path instead.
"""

_PLAN_CRAFT = """\
Write the plan for an implementer with zero context for the codebase and
questionable taste. A good plan makes implementation obvious — if someone has
to guess, the plan is incomplete.

Structure (include the sections that are relevant):
- Goal — one sentence.
- Current context / assumptions.
- Architecture / proposed approach — 2-3 sentences.
- Step-by-step tasks. Each task is bite-sized (2-5 minutes of focused work),
  names exact file paths (`src/models/user.py`, not "the model file"),
  includes complete copy-pasteable code where code is needed, and exact
  commands with expected output for verification.
- Tests / validation — for code tasks, follow the TDD cycle per task: write
  the failing test, run it to verify failure, implement minimally, run to
  verify pass, commit.
- Risks, tradeoffs, and open questions.

Principles: DRY, YAGNI, TDD, frequent commits. Avoid vague tasks ("add
authentication"), incomplete code ("add validation here"), and unverifiable
steps ("test it works" — instead: the exact command and its expected output).

Interaction style:
- If the request is clear enough, write the plan directly.
- If it is genuinely underspecified, ask a brief clarifying question instead
  of guessing.
- After saving the plan, reply briefly with what you planned and the saved
  path, and offer to execute it (e.g. via subagent-driven development) —
  but do not start executing in this turn.
"""


def build_plan_multistage_prompt(task: str = "") -> str:
    """Prompt for the multistage pipeline path of /plan (planning.multistage v0.5).
    The agent stays in its own runtime: it calls the plan_multistage tool, which
    orchestrates the five passes via subagents."""
    task_block = (
        f"Task to plan:\n{task}\n" if task.strip() else
        "No explicit task was given — infer the task from the current conversation context.\n"
    )
    return (
        "[/plan — multistage pipeline]\n\n"
        + task_block +
        "\nRun the `plan_multistage` tool now with exactly this task text.\n"
        "After it returns, report: (1) status (approved / needs_input / needs_revision),\n"
        "(2) the artifact directory, (3) the approved plan's tasks with their verification steps.\n"
        "If the result is needs_input, ask the user those blocking questions.\n"
        "Do not start implementing the plan in this turn."
    )


def build_plan_prompt(task: str = "") -> str:
    """Build the plan-mode prompt; empty *task* asks the agent to infer it from conversation context.

    See #36821.
    """
    task = (task or "").strip()
    task_block = f"Task to plan:\n{task}\n" if task else (
        "No explicit task was given with /plan — infer the task from the "
        "current conversation context (the thing we have been discussing "
        "or working toward). If the conversation does not imply a task, ask a brief clarifying question.\n"
    )
    return "[/plan — plan mode]\n\n" + _PLAN_MODE_RULES + "\n" + task_block + "\n" + _PLAN_CRAFT

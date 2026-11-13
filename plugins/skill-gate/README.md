# skill-gate

CI for the agent's skill set. Adapter tool (`skill_gate`) around the wiki-gate
engine (outer contour repo: `~/projects/self-improvement-watch/wiki-gate/`).

- `skill_gate(action='check')` — re-measures the accepted skill set against the
  bench's `best_val`; reports regressions per task. Run after ANY edit under
  `~/.hermes/skills` (own `skill_manage` edits, curator, nightly evolution).
- `skill_gate(action='status')` — evolution state: r_best, iteration history,
  holdout ids, model pin.

Contract honesty: a green check = 'no regression vs the measured best on the
model pinned in the engine (WG_MODEL)'. It is not proof a skill is good, and it
does not measure the live multi-turn agent (that's the multi-turn gate design:
self-improvement-watch/docs/plans/).

Engine path override: `skill_gate.engine_dir` in config.yaml. Absent engine =
tool reports 'engine absent' (fail-open, never breaks the session).

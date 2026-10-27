"""hermes_rescue — the task-dedicated immune core of hermes-autonomous.

Second kernel in the same release: ~300 lines, stdlib+httpx only, one job —
give the main autonomous node a *reasonable* rollback when it is broken:
inspect (read-only) → decide (LLM with deterministic fallback) → act through
a fixed hands kit → observe → report to the owner → hand back.

It has no skills, no MCP, no cron, no autonomy pump, no plugins, by
construction (not by config). It must not share a failure domain with the
gateway machinery it rescues, so it must never import ``gateway.*`` or
``agent.*``. Its only persistent state is an append-only journal and its own
HOME directory; the main node's state.db is touched only through snapshots.
"""

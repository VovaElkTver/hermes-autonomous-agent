"""``/for`` gateway handler: force a catalog through the controller queue.

Every entry of the target directory/glob becomes one ctl_step of a single
draft task; the workdir gets a ``user-command`` autonomy grant; the pump then
drives items in order. ``--go`` embeds the agreement in the command line
(it transitions the task to ready immediately). Pure queue mechanics — no
per-catalog script is written or run.
"""
from __future__ import annotations


class GatewayForEachCommandsMixin:
    """Slash surface for /for."""

    async def _handle_for_command(self, event) -> str:
        """Handle ``/for <path|glob> [instruction...] [--limit N] [--go]``."""
        from hermes_cli.foreach_queue import build_plan, parse_for_args

        arg = event.get_command_args().strip()
        try:
            parsed = parse_for_args(arg)
        except ValueError as exc:
            return f"❌ /for: {exc}"

        try:
            plan = build_plan(parsed["path"], parsed["instruction"], parsed["limit"])
        except ValueError as exc:
            return f"❌ /for: {exc}"

        from controller import model as ctl_model
        from controller import transitions

        with ctl_model.connect() as conn:
            tid = ctl_model.create_task(conn, owner="agent", workdir=plan.workdir,
                                        goal=plan.goal)
            fields = ctl_model.current_contract(conn, tid)
            fields["acceptance"] = plan.acceptance
            ctl_model.revise_contract(conn, tid, fields, "MATERIAL")
            for step in plan.steps:
                ctl_model.add_step(conn, tid, step["kind"], step["title"])
            perm = ctl_model.set_permission(conn, plan.workdir, enabled=True,
                                            granted_via="user-command")
            started = False
            if parsed["go"]:
                # The --go flag on the command line IS the agreement: same
                # words as «иди», embedded in the user's own command.
                ctl_model.agree_contract(conn, tid)
                transitions.transition(
                    conn, tid, "ready",
                    basis="/for --go (agreement in command line)",
                    permission=perm, expected_epoch=perm["execution_epoch"])
                started = True
        note = f" (+{plan.truncated} beyond --limit)" if plan.truncated else ""
        tail = (
            "Started on ready — the pump takes it step by step."
            if started else
            "Task lands as draft: the pump starts after the human agrees "
            f"(иди {tid})."
        )
        return (
            f"⛓ /for queued: task `{tid}` — {len(plan.steps)} step(s){note}\n"
            f"workdir: `{plan.workdir}` · permission enabled @ epoch "
            f"{perm['execution_epoch']} (user-command)\n"
            "Each catalog item is one queue step; batch scripts over the "
            "catalog are forbidden by the contract. " + tail
        )

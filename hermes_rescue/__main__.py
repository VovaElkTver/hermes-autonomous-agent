"""CLI: python -m hermes_rescue run-once [--dry-run] [--report]

Trigger model: the systemd detector raises hermes-rescue.service which runs
`run-once --report` (hands ON). `--dry-run` is the peace-time exercise mode:
full dossier + real decision, but every irreversible op is only journaled.
"""
from __future__ import annotations

import argparse
import logging
import sys

from . import ops
from .config import RescueConfig
from .decide import decide

log = logging.getLogger("hermes_rescue")


def run_once(cfg: RescueConfig, report: bool) -> int:
    from .inspect import inspect_main
    from .report import send
    dossier = inspect_main(cfg)
    verdict = decide(cfg, dossier)
    action, reason = verdict.get("action", "escalate"), verdict.get("reason", "")
    lines = [f"hermes_rescue: {action} — {reason}"]
    if action == "none":
        outcome = "main healthy, no action"
    elif action == "restart_unit":
        outcome = ops.restart_unit(cfg)
        obs = ops.observe(cfg)
        lines.append(f"post-restart healthy: {obs.get('healthy')}")
    elif action == "snapshot_db":
        outcome = ops.snapshot_db(cfg)
        lines.append("snapshot taken; re-decide needed on next run")
    elif action == "switch_release":
        ops.snapshot_db(cfg)
        outcome = ops.switch_release(cfg, verdict.get("release", ""))
        if not outcome.startswith("refused"):
            obs = ops.observe(cfg)
            lines.append(f"post-switch healthy: {obs.get('healthy')}")
    elif action == "lift_estop":
        outcome = ops.lift_estop(cfg)
    else:
        outcome = ops.escalate(cfg, reason)
    lines.append(f"detail: {outcome}")
    text = "\n".join(lines)
    print(text)
    if report:
        send(cfg, text)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="hermes_rescue")
    ap.add_argument("mode", choices=("run-once",))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(name)s %(levelname)s %(message)s")
    cfg = RescueConfig.load()
    if args.dry_run:
        cfg.dry_run = True
    return run_once(cfg, report=args.report)


if __name__ == "__main__":
    raise SystemExit(main())

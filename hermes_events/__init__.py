"""hermes_events — internal event processor with its own time registrar (v0).

The kernel's answer to cron: handlers register declaratively against a single
absolute clock; a tick matches ``now() after <t>`` and enqueues work on a
separate execution queue. Key semantics (contract agreed 26.10):

* time is ABSOLUTE (system epoch). Relative registration ("in 1h") is sugar of
  the registrar: it resolves to an absolute ``t`` at registration time.
* firing != executing. The tick only enqueues; an executor consumes the queue.
* auto-rearm is armed ONLY after the handler completed: the next ``t`` is
  computed from the completion instant, never from the planned one. This gives
  free backpressure — a slow handler stretches its own period, no overlap, no
  catch-up avalanche after downtime.
* one-shot events are removed (journal-traced) after execution.
* service events never touch the queue and never wake the LLM — they run
  inline in the tick body.
* the queue dedups per handler id: while an execution is in flight (or already
  pending), a re-fire marks ``wait_again`` instead of cloning work.
* the processor must NOT write to agent memory; it journals to jsonl only.
"""

from hermes_events.clock import Clock, now
from hermes_events.registry import EventKind, Registry, parse_when
from hermes_events.processor import EventProcessor

__all__ = ["Clock", "EventKind", "EventProcessor", "Registry", "now", "parse_when"]

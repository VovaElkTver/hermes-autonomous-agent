"""Single-query (``-q`` / one-shot) helpers: exit-code mapping, quiet single-query runner, image routing, signal handlers and the single-query mode orchestrator.

Split out of ``cli.py``; ``cli`` re-exports every public name and moved bodies late-bind
cli-level names through ``from cli import ...`` at call time so facade monkeypatch seams hold.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import logging
import os
import sys
import time
from agent.i18n import t
from agent.interrupt_compat import request_hard_interrupt
from contextlib import suppress
from pathlib import Path
from typing import Any

# Log-record parity with the origin module.
logger = logging.getLogger("cli")

if TYPE_CHECKING:
    from cli import HermesCLI


def _int_or(value, default: int) -> int:
    """``int(value)``, or ``default`` when it does not parse."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _interrupt_agent_for_signal(agent, signum) -> None:
    """Hard-interrupt ``agent`` for a shutdown signal, then sleep ``HERMES_SIGTERM_GRACE`` (1.5 s).

    The grace lets the agent thread kill the tool's setsid subprocess group before the
    main thread unwinds (else an orphan child). Never raises.
    """
    from cli import _float_env
    try:
        if agent is not None:
            request_hard_interrupt(agent, f"received signal {signum}")
            _grace = _float_env("HERMES_SIGTERM_GRACE", 1.5)
            if _grace > 0:
                time.sleep(_grace)
    except Exception:
        pass  # never block signal handling


def _sync_cli_session_id_from_agent(cli) -> None:
    """Keep ``cli.session_id`` in sync when mid-run compression rotated the agent's session."""
    if getattr(cli.agent, "session_id", None) and cli.agent.session_id != cli.session_id:
        cli.session_id = cli.agent.session_id


# ``failure_reason`` values that say nothing about the task itself: the provider is walled,
# down or unreachable, or the account is out of credit.
from hermes_cli.turn_exit import TRANSIENT_PROVIDER_REASONS as _TRANSIENT_PROVIDER_REASONS


# ``failure_reason`` values a retry can never heal: the credential was rejected, the model does
# not exist for this account, or the TLS chain is broken. ``billing`` stays transient: credit
# comes back. ``upstream_blocked`` (a WAF/CDN refusing the SDK's User-Agent) is terminal too:
# only a header change heals it, never a retry.
from hermes_cli.turn_exit import TERMINAL_PROVIDER_REASONS as _TERMINAL_PROVIDER_REASONS


def _single_query_exit_code(result, *, credentials_rate_limited: bool = False,
                            credentials_terminal: bool = False) -> int:
    """Map a one-shot turn result onto a process exit code, for both `-q` and `-Q`.

    0 only when the turn completed; 130 when it was interrupted; 1 when it failed, stopped
    partway (`partial`, `completed: False`) or never ran at all (credentials / agent init
    failed, so ``result`` is not a dict).
    """
    # Late-bound through the ``cli`` facade so a patched ``cli._*_PROVIDER_REASONS`` holds.
    from cli import _TERMINAL_PROVIDER_REASONS as terminal_reasons
    from cli import _TRANSIENT_PROVIDER_REASONS as transient_reasons
    from hermes_cli.turn_exit import turn_exit_code
    return turn_exit_code(
        result,
        credentials_rate_limited=credentials_rate_limited,
        credentials_terminal=credentials_terminal,
        transient_reasons=transient_reasons,
        terminal_reasons=terminal_reasons,
    )


def _run_quiet_single_query(cli, effective_query, emitter=None):
    """Quiet (-Q) one-shot turn: run, print the response (stderr for errors/session_id), then sys.exit with the automation exit code.
    With a ``StreamJsonEmitter`` the final answer and the exit line become the terminal ``result`` JSONL record instead.
    HERMES_TURN_AUTHOR (set only by a bot-to-bot dispatcher) is consumed here so tool subprocesses do not inherit it.
    Nested Bot Mode notifies bind this session's key (not the dispatcher's) and resume in-process
    before stdout is printed, so a teammate reply is the quiet run's final answer rather than a
    stranded receipt."""
    from cli import _emit_interrupted_session_end, _single_query_exit_code, _sync_cli_session_id_from_agent
    from agent.interrupt_compat import _accepts_keyword
    from agent.turn_author import take_turn_author_from_env
    from hermes_cli.quiet_single_query import (
        bind_quiet_session_key, continue_quiet_notify_completions, exit_single_query, quiet_notify_linger_seconds, take_turn_report_path, write_turn_report,
    )

    author = take_turn_author_from_env()
    # A spawner that bounds only the turn (scheduled Bot Chat lane) learns the outcome from this
    # report, written before the linger below; popped so tool subprocesses do not inherit it.
    turn_report_path = take_turn_report_path()
    author_kwargs = {"turn_author": author} if author is not None and _accepts_keyword(cli.agent.run_conversation, "turn_author") else {}
    with bind_quiet_session_key(getattr(cli, "session_id", "") or "default"):
        try:
            result = cli.agent.run_conversation(
                user_message=effective_query, conversation_history=cli.conversation_history, **author_kwargs,
            )
        except KeyboardInterrupt:
            _emit_interrupted_session_end(cli, reason="keyboard_interrupt")
            if emitter is not None:
                exit_single_query(emitter.emit_result({"failed": True, "error": "Interrupted"}, session_id=cli.session_id or "", exit_code=130))
            print(f"\nsession_id: {cli.session_id}", file=sys.stderr)
            exit_single_query(130)
        # The exit line below reports session_id to stderr for automation wrappers;
        # without this sync it would point at the ended parent after compression.
        _sync_cli_session_id_from_agent(cli)
        # The turn is over and persisted: the one-shot exit linger that follows protects nested
        # notify_on_complete replies and is NOT part of the spawner's delivery (#113608). The
        # report carries what this run will print, so a spawner booking a child still lingering
        # at its cap relays the answer instead of a timeout (#114980).
        def _report_turn(res) -> None:
            write_turn_report(
                turn_report_path, exit_code=_single_query_exit_code(res),
                error=str(res.get("error") or "") if isinstance(res, dict) else "agent turn did not run",
                reply=res.get("final_response", "") if isinstance(res, dict) else str(res),
            )

        _report_turn(result)
        if isinstance(result, dict) and not result.get("failed"):
            history = result.get("messages") or cli.conversation_history

            def _follow_up(text):
                nonlocal history
                follow = cli.agent.run_conversation(
                    user_message=text, conversation_history=history, **author_kwargs,
                )
                if isinstance(follow, dict) and follow.get("messages"):
                    history = follow["messages"]
                # Same sync contract as the main turn: a compression rotation during a
                # follow-up must not leave a stale id on the exit line / drain key.
                _sync_cli_session_id_from_agent(cli)
                return follow

            # One shared linger budget for the whole run: the loop below and the later
            # _wait_for_oneshot_background_completions pass must not each wait the full
            # oneshot_completion_wait_seconds on the same stuck notify_on_complete child.
            # Flagged after the loop (finally-equivalent): the wait is the loop's first
            # statement, so anything raising past that point has consumed budget the
            # finalize pass must not re-wait.
            try:
                continued = continue_quiet_notify_completions(
                    getattr(cli, "session_id", "") or "",
                    _follow_up,
                    owns_event=getattr(cli, "_owns_process_notification", None),
                    linger_budget=quiet_notify_linger_seconds(),
                )
            finally:
                cli._quiet_notify_linger_done = True
            if isinstance(continued, dict):
                result = continued
                # A teammate's reply displaced the answer this run prints; tell the spawner.
                _report_turn(result)
        response = result.get("final_response", "") if isinstance(result, dict) else str(result)
    # Surface backend errors that produced no visible output (e.g. invalid model slug
    # -> provider 4xx) on stderr so piped stdout stays clean.
    if emitter is not None:
        pass  # the result record below carries text/error; nothing else may touch stdout
    elif (
        not response and isinstance(result, dict) and result.get("error")
        and (result.get("failed") or result.get("partial"))
    ):
        print(t("gateway.model.error_prefix", error=result["error"]), file=sys.stderr)
    elif response:
        print(response)

    # ONE exit-code decision point for this driver, shared with the non-quiet one-shot
    # path via _single_query_exit_code.
    _exit_code = _single_query_exit_code(result)

    if emitter is None:
        print(f"\nsession_id: {cli.session_id}", file=sys.stderr)

    if emitter is not None:
        _exit_code = emitter.emit_result(result, session_id=cli.session_id or "", exit_code=_exit_code)
    exit_single_query(_exit_code)


def _route_single_query_images(cli, query, effective_query, single_query_images, single_query_image_urls):
    """Attach one-shot images natively when the model supports vision, else pre-describe them as text."""
    if not (single_query_images or single_query_image_urls):
        return effective_query
    # Same image-routing decision as the interactive path: a vision-capable model
    # (incl. custom-provider models declaring `model.supports_vision: true`) gets
    # native image_url parts; otherwise the text pipeline (vision_analyze
    # pre-description).
    _img_mode = "text"
    _build_parts = None
    try:
        from agent.image_routing import build_native_content_parts as _build_parts
        from agent.image_routing import decide_image_input_mode
        from hermes_cli.config import load_config

        _img_mode = decide_image_input_mode(
            (cli.provider or "").strip(), (cli.model or "").strip(), load_config(),
            requested_provider=(cli.requested_provider or "").strip(),
        )
    except Exception:
        _img_mode = "text"

    def _text_fallback():
        # ``_preprocess_images_with_vision`` only knows local files; when only URLs
        # were supplied keep the original query text intact.
        if single_query_images:
            return cli._preprocess_images_with_vision(query, single_query_images, announce=False)
        return effective_query

    if _img_mode != "native" or _build_parts is None:
        return _text_fallback()
    try:
        _parts, _skipped = _build_parts(
            query if isinstance(query, str) else "",
            [str(p) for p in single_query_images],
            image_urls=list(single_query_image_urls) or None,
        )
        if any(p.get("type") == "image_url" for p in _parts):
            return _parts
        return _text_fallback()  # all images unreadable
    except Exception:
        return _text_fallback()


def _install_single_query_signal_handlers(cli):
    """Route SIGINT/SIGTERM/SIGHUP through agent.interrupt() before unwinding; kanban workers hard-exit.

    A plain KeyboardInterrupt only unwinds the main thread, so tool worker threads
    would orphan the setsid child; the interrupt + grace window lets them kill it.
    """
    from cli import _arm_exit_watchdog_on_shutdown_signal, _flush_logging_and_stdio, _flush_one_shot_session_store, _interrupt_agent_for_signal
    import signal as _signal

    def _kill_foreground_and_exit(*_):
        # The worker's command runs in its own process group: SIGKILL it or it outlives os._exit.
        with suppress(Exception):
            from tools.environments.base import kill_live_foreground_processes
            kill_live_foreground_processes(now=True)
        os._exit(0)

    def _signal_handler_q(signum, frame):
        logger.debug("Received signal %s in single-query mode", signum)
        _arm_exit_watchdog_on_shutdown_signal()  # covers wedges in the unwind below
        _interrupt_agent_for_signal(getattr(cli, "agent", None), signum)
        # Kanban: a non-daemon worker blocked in _wait_for_process survives KeyboardInterrupt
        # and the dispatcher sees 'running' forever, so os._exit(0) (SIGALRM deadman guards
        # a blocking flush). That skips atexit + the token-drain hook, hence the explicit flush.
        # Kanban worker exit path (#28181): SIGTERM hits a dispatcher-spawned worker that's likely in a
        # non-daemon thread waiting on a child subprocess in _wait_for_process. Raising KeyboardInterrupt
        # only unwinds the main thread; the worker thread keeps running, the process gets reparented to
        # init, and the dispatcher's _pid_alive check returns True forever — task stuck in 'running'
        # indefinitely. Skip the controlled-unwind dance and call os._exit(0) so the kernel reclaims the PID
        # immediately and detect_crashed_workers can reclaim the stale claim on the next tick. Flush logging
        # + stdout/stderr first so the final debug trace isn't lost; SIGALRM deadman guards the flush
        # against any rare blocking-I/O case (the reporter measured flush in <1ms; the alarm is a failsafe,
        # not the common path).
        if os.environ.get("HERMES_KANBAN_TASK"):
            with suppress(Exception):
                if hasattr(_signal, "SIGALRM"):
                    _signal.signal(_signal.SIGALRM, _kill_foreground_and_exit)
                    _signal.alarm(5)
            with suppress(Exception):
                # Durable flush FIRST: memory-provider shutdown inside _run_cleanup can issue aux-LLM calls,
                # and nothing after it may fail in a way that loses the turn (#88583).
                # os._exit(0) skips atexit AND SessionDB's token-drain hook, so flush + finalize the session
                # store here or the worker's turn (and its usage deltas) never become durable (#88583 /
                # #50881 class). Best-effort under the SIGALRM deadman above.
                _flush_one_shot_session_store(cli)
            # The one-shot exit's shared_metrics shutdown_runtimes is skipped too: bounded inside the deadman.
            from hermes_cli.observability.shared_metrics_process import flush_before_hard_exit
            flush_before_hard_exit()
            _flush_logging_and_stdio()
            _kill_foreground_and_exit()
        raise KeyboardInterrupt()
    with suppress(Exception):  # restricted environments
        for _name in ("SIGINT", "SIGTERM", "SIGHUP"):
            if hasattr(_signal, _name):
                _signal.signal(getattr(_signal, _name), _signal_handler_q)


def _configure_quiet_agent(agent) -> None:
    """Neutralize every stdout-writing callback so -Q stdout carries only the final response."""
    agent.quiet_mode = True
    agent.suppress_status_output = True
    agent.stream_delta_callback = None
    agent.tool_gen_callback = None
    agent.reasoning_callback = None
    # The diff/progress callbacks print directly and are gated by neither quiet_mode nor
    # tool_progress_mode, so they must go too; "off" also covers the executor's direct prints.
    agent.tool_progress_callback = None
    agent.tool_start_callback = None
    agent.tool_complete_callback = None
    agent.tool_progress_mode = "off"


def _run_single_query_mode(cli, query, image, quiet, oneshot, stream_json: bool = False):
    """``-q``/``--image`` entry: seed an interactive session on a TTY, else run the one-shot turn and exit.
    ``stream_json`` (implies quiet) swaps the plain-text final answer for the JSONL event protocol."""
    from cli import _SeededQueryMessage, _collect_query_images, _configure_quiet_agent, _finalize_single_query, _route_single_query_images, _run_quiet_single_query, _should_seed_interactive, _single_query_exit_code
    if _should_seed_interactive(query, image, quiet, oneshot):
        seeded_query, seeded_images = _collect_query_images(query, image)
        logger.info(
            "Seeding interactive session with -q prompt (%d chars, %d images)",
            len(seeded_query or ""), len(seeded_images),
        )
        cli._seeded_first_message = _SeededQueryMessage(seeded_query, seeded_images)
        return cli.run()
    cli._single_query_mode = True  # agent waits the full MCP cold-start before its only tool snapshot
    # No PluginManager._cli_ref (#67597): the turn's plugin tools run in the gateway, and this process
    # loads no plugins (PLG-7).
    # No user can answer approval prompts: the approval gate takes the deterministic path.
    # One-shot mode: no between-turns MCP late-binding refresh, so the agent must wait the full MCP
    # cold-start bound before its first (and only) tool snapshot. See #51316.
    # Mark single-query for the approval gate. cli.py sets HERMES_INTERACTIVE earlier for interactive sudo
    # prompts, but a -q run has NO user waiting to answer approval prompts. The gate reads this marker (via
    # gateway.session_context.get_session_env, which falls back to os.environ when the session-context layer
    # isn't engaged) and takes the deterministic approvals.single_query_mode path instead of waiting the
    # full timeout. See #86878.
    os.environ["HERMES_SINGLE_QUERY_SESSION"] = "1"
    from hermes_cli.quiet_single_query import exit_single_query
    if not cli._claim_active_session("cli", stderr=bool(quiet)):
        exit_single_query(1)
    try:
        query, single_query_images = _collect_query_images(query, image)
        single_query_image_urls: list[str] = []
        from hermes_cli.observability.shared_metrics_startup import record_cli_one_shot_ready
        record_cli_one_shot_ready()
        if quiet:
            # Quiet mode: suppress banner, spinner, tool previews.
            cli.tool_progress_mode = "off"
            emitter = None
            if stream_json:
                # Built BEFORE credentials/agent init so a failed start still closes the protocol
                # (init + result) instead of exiting 1 with an empty stdout.
                from hermes_cli.stream_json import StreamJsonEmitter
                emitter = StreamJsonEmitter(model=getattr(cli, "model", "") or "", session_id=cli.session_id or "")
            if cli._ensure_runtime_credentials():
                effective_query: Any = _route_single_query_images(
                    cli, query, query, single_query_images, single_query_image_urls
                )
                turn_route = cli._resolve_turn_agent_config(effective_query)
                if turn_route["signature"] != cli._active_agent_route_signature:
                    cli.agent = None
                if cli._init_agent(
                    model_override=turn_route["model"],
                    runtime_override=turn_route["runtime"],
                    request_overrides=turn_route.get("request_overrides"),
                ):
                    _configure_quiet_agent(cli.agent)
                    if emitter is not None:
                        emitter.attach(cli.agent)
                    _run_quiet_single_query(cli, effective_query, emitter=emitter)

            fail_code = _single_query_exit_code(
                None, credentials_rate_limited=getattr(cli, "_credentials_rate_limited", False),
                credentials_terminal=getattr(cli, "_credentials_terminal", False))
            if emitter is not None:
                emitter.emit_result({"failed": True, "error": "credentials or agent init failed"},
                                    session_id=cli.session_id or "", exit_code=fail_code)
            exit_single_query(fail_code)  # credentials or agent init failed
        # No welcome banner (~420 ms cold); session id / resume hint come from _print_exit_summary().
        _query_label = query or ("[image attached]" if single_query_images else "")
        if _query_label:
            cli.console.print(f"[bold blue]{t('cli.single_query.query_label')}[/] {_query_label}")
        cli._show_security_advisories()
        # Same author contract as `-Q`: a spawner that sets HERMES_TURN_AUTHOR names who wrote this run's
        # prompts. Consumed before the first turn so tool subprocesses never inherit it; every turn of
        # the run (recovery and goal follow-ups included) carries it to memory providers.
        from agent.turn_author import take_turn_author_from_env
        cli._single_query_turn_author = take_turn_author_from_env()
        response = cli.chat(query, images=single_query_images or None)
        cli._print_exit_summary(clear_screen=False)
        # Same exit contract as `-Q`: scripts and the Kanban dispatcher read the outcome from
        # the exit code. This path used to fall through to an implicit 0 for every outcome.
        exit_single_query(_single_query_exit_code(
            cli._last_turn_result,
            credentials_rate_limited=getattr(cli, "_credentials_rate_limited", False),
            credentials_terminal=getattr(cli, "_credentials_terminal", False)))
    finally:
        _finalize_single_query(cli)

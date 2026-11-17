"""Autonomy-loop gateway commands: /goal, /subgoal, /heartbeat, /loop, /refine, /review.
Bound onto ``GatewayRunner`` through ``GatewaySlashCommandsMixin``."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import subprocess
import sys
from pathlib import Path

from agent.i18n import t
from gateway.platforms.event import MessageEvent, MessageType

# Log-record parity with gateway/run.py and the origin module.
logger = logging.getLogger("gateway.run")


def _spawn_dream_process(cmd: list[str], log_path, cwd) -> int:
    """Blocking spawn of the detached dream0 run; executed via asyncio.to_thread."""
    with open(log_path, "ab") as log_f:
        proc = subprocess.Popen(cmd, stdout=log_f, stderr=log_f,
                                cwd=str(cwd), start_new_session=True)
    return proc.pid


def _dream_pid_alive(pid: int) -> bool:
    """Cross-platform liveness probe.

    POSIX: os.kill(pid, 0) raises only if the process is gone (or access fails).
    Windows: os.kill() with any signal TERMINATES the process (TerminateProcess),
    so it must never be used as a probe — psutil.pid_exists is non-destructive.
    """
    if pid <= 0:
        return False
    try:
        import psutil  # type: ignore
    except ImportError:
        return True  # cannot probe safely — assume alive, fail closed on the guard
    try:
        return bool(psutil.pid_exists(pid))
    except Exception:
        return True

def _emit_dream_notice(rec: dict) -> None:
    """Append a scheduled marker so the outer contour stays the single ledger writer."""
    try:
        ledger = Path.home() / "projects" / "outer-contour" / "dream0" / "data" / "dream-ledger.jsonl"
        with open(ledger, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": rec["started"], "kind": "scheduled_by_command",
                                 "pid": rec["pid"], "span": rec["span"]}) + "\n")
    except OSError:
        logger.debug("dream: ledger notice failed", exc_info=True)


def _quiet_bool(fn) -> bool:
    try:
        return bool(fn())
    except Exception:
        return False


def _mgr_call(prefix: str, fn, *args, errors=(RuntimeError, ValueError)):
    """``(result, None)`` from ``fn(*args)``, or ``(None, "<prefix>: <exc>")`` on a manager error."""
    try:
        return fn(*args), None
    except errors as exc:
        return None, f"{prefix}: {exc}"


class GatewayGoalCommandsMixin:
    """Autonomy-loop gateway commands: /goal, /subgoal, /heartbeat, /loop, /refine, /review."""

    async def _handle_goal_command(self, event: MessageEvent) -> str:
        from hermes_cli.goal_command import dispatch_goal_command
        from hermes_cli.goals import last_user_message_from_db

        mgr, _session_entry = await self._get_goal_manager_for_event(event)
        if mgr is None:
            return t("gateway.goal.unavailable")

        def authorize_gate():
            # A local session's owner is its admin: slash.exec admitted this command only after
            # authorize_local_source and the session:control capability (T7). Messaging stays
            # explicit-admin-only.
            from gateway.session_local import authorize_local_source
            if event.source is not None and authorize_local_source(self, event.source) is True:
                return None
            if not self._resume_caller_is_admin(event.source):
                return t("gateway.goal.gate_add_admin_only")
            return None

        def dispatch():
            return dispatch_goal_command(
                mgr, event.get_command_args() or "", authorize_gate=authorize_gate,
                render=lambda key, default, **values: t(key, **values),
                last_user_message=last_user_message_from_db(getattr(mgr, "session_id", None)),
            )

        # Drafting resolves profile-scoped credentials. Keep ContextVars across the
        # executor hop; manager I/O must also stay off the messaging event loop.
        result = await self._run_in_executor_with_context(dispatch)
        if result.clear_pending:
            self._clear_goal_continuations(event, result.clear_pending)
        if result.prompt:
            self._enqueue_goal_turn(event, result.prompt, label="command enqueue", kickoff=result.kickoff)
        return result.output

    def _clear_goal_continuations(self, event: MessageEvent, verb: str) -> None:
        try:
            adapter, quick_key = self._adapter_and_key_for(event)
            if adapter and quick_key:
                self._clear_goal_pending_continuations(quick_key, adapter)
        except Exception as exc:
            logger.debug("goal %s: pending continuation cleanup failed: %s", verb, exc)

    def _enqueue_goal_turn(
        self, event: MessageEvent, text: str, *, label: str, kickoff: bool
    ) -> None:
        """Enqueue *text* as the next turn through the adapter FIFO (the post-turn judge's path).

        A kickoff keeps the triggering message id / channel prompt / bound skills; a resume
        continuation carries none. Best-effort: failures only logged. A local ``slash.exec`` returns
        the turn to the client as its ``send`` directive instead (the client submits it with its own
        input id).
        """
        from gateway.session_command_sink import active_sink
        if (sink := active_sink()) is not None:
            sink.prompt = text
            if not kickoff:
                sink.display = "/goal resume"
            return
        try:
            adapter, quick_key = self._adapter_and_key_for(event)
            if text and adapter and quick_key:
                turn = MessageEvent(
                    text=text,
                    message_type=MessageType.TEXT,
                    source=event.source,
                    message_id=event.message_id if kickoff else None,
                    edit_revision=event.edit_revision if kickoff else None,  # an edited /goal: own admission id
                    channel_prompt=event.channel_prompt if kickoff else None,
                    auto_skill=event.auto_skill if kickoff else None,
                    inherit_channel_pin=not kickoff,
                )
                self._enqueue_fifo(quick_key, turn, adapter)
        except Exception as exc:
            logger.debug("goal %s failed: %s", label, exc)

    async def _handle_heartbeat_command(self, event: MessageEvent) -> str:
        """Handle /heartbeat (mirror of the CLI handler): the session's one recurring re-entry
        prompt. The gateway-wide poller injects due heartbeats through the adapter FIFO as
        ordinary user turns, so alternation and caching hold."""
        from hermes_cli.heartbeat import parse_interval, format_interval, MIN_INTERVAL_SECONDS
        args = (event.get_command_args() or "").strip()
        lower = args.lower()
        mgr, _session_entry = await self._get_heartbeat_manager_for_event(event)
        if mgr is None:
            return t("gateway.heartbeat.unavailable")
        quick_key = self._session_key_for_source(event.source) if event.source else None

        def _watch():
            if quick_key and event.source is not None:
                self._register_heartbeat_watch(quick_key, event.source, mgr.session_id)

        if not args or lower == "status":
            return mgr.status_line()
        if lower == "pause":
            state = mgr.pause()
            return t("gateway.heartbeat.paused", prompt=state.prompt) if state else t("gateway.heartbeat.none_set")
        if lower == "resume":
            state = mgr.resume()
            if state is None:
                return t("gateway.heartbeat.no_resume")
            _watch()
            return t("gateway.heartbeat.resumed", interval=format_interval(state.interval_seconds), prompt=state.prompt)
        if lower in {"clear", "stop", "off"}:
            had = mgr.clear()
            if quick_key:
                self._unregister_heartbeat_watch(quick_key)
            return t("gateway.heartbeat.cleared") if had else t("gateway.heartbeat.none_set")

        # Set: `/heartbeat every 10m <prompt>` (also accepts `10m <prompt>`).
        tokens = args.split(None, 2)
        interval, prompt = None, ""
        if tokens[0].lower() == "every" and len(tokens) >= 2:
            interval = parse_interval(f"every {tokens[1]}")
            prompt = tokens[2] if len(tokens) > 2 else ""
        else:
            interval = parse_interval(tokens[0])
            prompt = args[len(tokens[0]):].strip() if interval and interval > 0 else ""
        if interval is None:
            return t("gateway.heartbeat.usage")
        if interval < 0:
            return t("gateway.heartbeat.interval_too_small", min_seconds=MIN_INTERVAL_SECONDS)
        if not prompt.strip():
            return t("gateway.heartbeat.prompt_required")
        state, err = _mgr_call(t("gateway.heartbeat.invalid_prefix"), mgr.set, prompt, interval, errors=(ValueError,))
        if err:
            return err
        _watch()
        return t("gateway.heartbeat.set", interval=format_interval(state.interval_seconds), prompt=state.prompt)

    def _idle_cached_agent_or_error(self, event: MessageEvent, verb: str):
        """``(session_key, cached_agent, None)`` for /refine and /review, or ``(_, _, error_text)``:
        both need a cached agent from a completed turn and refuse while a run is in flight.
        ``verb`` is the command name (``refine`` / ``review``) and selects ``gateway.<verb>.*`` copy."""
        quick_key = self._session_key_for_source(event.source) if event.source else None
        if not quick_key:
            return None, None, t(f"gateway.{verb}.unavailable")
        if quick_key in self._running_agents:
            return quick_key, None, t("gateway.shared.agent_running_retry_later", command=verb)
        agent = self._cached_agent_for(quick_key)
        if agent is None:
            return quick_key, None, t(f"gateway.{verb}.nothing_yet")
        return quick_key, agent, None

    async def _handle_dream_command(self, event: MessageEvent) -> str:
        """Handle /dream — thin gateway adapter to the outer dream0 contour.

        The contour lives OUTSIDE the agent repo (outer-contour, DESIGN caveat #0); this
        command only spawns its CLI and reads its artifacts: ledger + need2write.md.
        """
        args = (event.get_command_args() or "").strip()
        verb, _, rest = args.partition(" ")
        verb = verb.strip().lower() or "status"
        rest = rest.strip()

        ledger = Path.home() / "projects" / "outer-contour" / "dream0" / "data" / "dream-ledger.jsonl"
        script = Path.home() / "projects" / "outer-contour" / "dream0" / "dream0.py"
        pid_file = Path.home() / ".hermes" / "cache" / "scratch" / "dream_run.json"

        def _tail_ledger(n=6):
            try:
                lines = ledger.read_text(encoding="utf-8").strip().splitlines()
                return [json.loads(l) for l in lines[-n:] if l.strip()]
            except (OSError, ValueError):
                return []

        if verb in ("go", "иди"):
            if not script.exists():
                return "dream0: скрипт контура не найден — /dream недоступен."
            # one live run at a time; a stale pid (no such process) does not block a rerun
            if pid_file.exists():
                try:
                    old = json.loads(pid_file.read_text(encoding="utf-8"))
                    if _dream_pid_alive(int(old["pid"])):
                        return f"[dream] прогон уже идёт (PID {old['pid']}, от {old.get('started','?')}); /dream status."
                except (OSError, ValueError, KeyError):
                    pass  # unreadable record — treat as stale, allow a new run
            span = int(rest) if rest.isdigit() and int(rest) > 0 else 0
            log_path = Path.home() / ".hermes" / "logs" / f"dream-run-{dt.datetime.now():%Y%m%d-%H%M%S}.log"
            cmd = [sys.executable, str(script), "--stage", "all", "--span", str(span)]
            proc_pid = await asyncio.to_thread(_spawn_dream_process, cmd, log_path, script.parent)
            run_rec = {"pid": proc_pid, "started": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                       "span": span or "rand1-5", "log": str(log_path)}
            pid_file.parent.mkdir(parents=True, exist_ok=True)
            pid_file.write_text(json.dumps(run_rec), encoding="utf-8")
            await asyncio.to_thread(_emit_dream_notice, run_rec)
            span_txt = run_rec["span"]
            return (f"[dream] сон запущен (PID {proc_pid}, окно {span_txt} сут., "
                    f"log: {log_path.name}); вердикты появятся в need2write после wake.")
        if verb == "last":
            woken = [e for e in _tail_ledger(20) if e.get("kind") == "woke"]
            if not woken:
                return "[dream] завершённых проб пока нет в леджере."
            e = woken[-1]
            return (f"[dream] wake {e.get('ts', '?')} день {e.get('day')}: "
                    f"verified {e.get('verified', '?')} / refuted {e.get('refuted', '?')} / "
                    f"probe_error {e.get('probe_error', '?')}, доставлено карточек: {e.get('cards', '?')} "
                    f"(всего {e.get('cards_total', '?')}).")
        # default: status
        entries = _tail_ledger(8)
        running = ""
        if pid_file.exists():
            try:
                old = json.loads(pid_file.read_text(encoding="utf-8"))
                if _dream_pid_alive(int(old["pid"])):
                    running = f"\nПрогон: PID {old['pid']} от {old.get('started','?')} (окно {old.get('span')})."
                else:
                    running = "\nАктивного прогона нет."
            except (OSError, ValueError, KeyError):
                running = "\nАктивного прогона нет."
        tail = "\n".join(f"{e.get('ts','?')} {e.get('kind','?')} {e.get('day','')}" for e in entries)
        return f"[dream] леджер (последние события):{running}\n{tail}"

    async def _handle_refine_command(self, event: MessageEvent) -> str:
        """Handle /refine — run the memory/skill review fork on demand, in a daemon thread against a
        snapshot of the cached AIAgent's conversation (live session and prompt cache untouched)."""
        args = (event.get_command_args() or "").strip()
        from gateway.refine_managed import (
            REFINE_GATE_TIMEOUT_S, handle_command as _managed_refine_cmd, managed_refine_enabled,
        )
        if managed_refine_enabled():
            _plat = getattr(getattr(event.source, "platform", None), "name", "local").lower()
            _ckey = f"{_plat}:{getattr(event.source, 'chat_id', '')}"
            _first, _, _rest = args.partition(" ")
            _first = _first.strip().lower()
            if _first in {"go", "иди", "stop", "стоп", "postpone", "отложи",
                          "approve", "reject", "status", "resume",
                          "topic-new", "topic-use"}:
                reply = _managed_refine_cmd(_ckey, _first, _rest.strip())
                if reply is not None:
                    return reply
                return "managed_refine: вердикт принят."
            if args:
                from gateway.refine_managed import _GATES, RUNS
                run_ = RUNS.get(_ckey)
                if run_ is not None and getattr(run_, "stage", "") == "TOPIC_CONFIRM":
                    # свободный текст на вопрос о теме = имя темы (ответ владельца)
                    return _managed_refine_cmd(_ckey, "topic:" + args, "", agent=None) \
                        or "[managed_refine] вердикт темы принят."
                gate = _GATES.get(_ckey)
                if gate is not None and gate.verdict is None and not _first.startswith("/"):
                    _managed_refine_cmd(_ckey, args, "", agent=None)  # свободный текст = поправка промпта (§2.1)
                    return f"[managed_refine] поправка промпта принята; жду вердикта или {REFINE_GATE_TIMEOUT_S}s тишины."
        quick_key, agent, error = self._idle_cached_agent_or_error(event, "refine")
        snapshot = list(getattr(agent, "_session_messages", None) or [])
        session_id = None
        if not snapshot and quick_key and quick_key not in self._running_agents:
            # A local session's conversation is its stored transcript, not the cached agent: /moa
            # (and its settle) drop that agent, so /refine reads the session itself (T7).
            from gateway.session_refine import refine_local_session
            local = await refine_local_session(self, event.source, args)
            if local is not None:
                return local
            # A released cached agent (idle TTL, pressure, /moa), or one holding no messages, is not
            # an empty conversation: refine the stored transcript.
            from gateway.session_cold_review import stored_transcript
            from gateway.session_transcript import TranscriptReadError
            from gateway.slash_commands_status import history_unreadable
            try:
                session_id, snapshot = await stored_transcript(self, quick_key)
            except TranscriptReadError:
                return history_unreadable()
            error = None if snapshot else error
        if error:
            return error
        if not snapshot:
            return t("gateway.refine.empty")
        try:
            if agent is None:
                from gateway.refine_managed import managed_refine_enabled as _mr_on
                if _mr_on():
                    return "[managed_refine] cold-путь (эвиicted агент / сохранённая транскрипт) во managed-режиме пока не поддержан — снимите /moa-арест агента или повторите после следующего хода."
                from gateway.session_cold_review import start_cold_refine
                await start_cold_refine(self, event.source, quick_key, session_id, snapshot, args)
            else:
                from gateway.refine_managed import managed_refine_enabled as _mr_on
                if _mr_on():
                    # Единый ключ гейтов с диспетчером: вердикты (/refine go|...) ищут прогон по нему.
                    agent.session_chat_id = (
                        f"{getattr(getattr(event.source, 'platform', None), 'name', 'local').lower()}:"
                        f"{getattr(event.source, 'chat_id', '')}")
                agent._spawn_background_review(
                    messages_snapshot=snapshot, review_memory=True,
                    review_skills="skill_manage" in getattr(agent, "valid_tool_names", set()), focus=args or None,
                    explicit=True,
                )
        except Exception as exc:
            return t("gateway.refine.start_failed", error=exc)
        tail = t("gateway.refine.focus_suffix", focus=args) if args else ""
        return t("gateway.refine.started", focus_suffix=tail)

    async def _handle_review_command(self, event: MessageEvent) -> str:
        """Handle /review — spawn an independent reviewer subagent. The approval session-key
        contextvar is only bound during agent turns, so bind it explicitly here or the completion
        event carries no gateway route and never re-enters this chat."""
        args = (event.get_command_args() or "").strip()
        quick_key, agent, error = self._idle_cached_agent_or_error(event, "review")
        snapshot = list(getattr(agent, "_session_messages", None) or [])
        cold_session_id = None
        if agent is None and quick_key and quick_key not in self._running_agents:
            # The cached agent was evicted (idle TTL, pressure, /moa): review the stored transcript.
            from gateway.session_cold_review import cold_review_history
            from gateway.session_transcript import TranscriptReadError
            from gateway.slash_commands_status import history_unreadable
            try:
                cold_session_id, snapshot = await cold_review_history(self, event.source)
            except TranscriptReadError:
                return history_unreadable()
            error = None
        if error:
            return error
        from tools.approval_context import reset_current_session_key, set_current_session_key

        def _dispatch():
            from agent.review_engine import start_review
            from gateway.session_cold_review import cold_review_parent
            token = set_current_session_key(quick_key)
            try:
                if cold_session_id is None:
                    return start_review(agent, snapshot, args)
                with cold_review_parent(self, event.source, quick_key, cold_session_id) as parent:
                    return start_review(parent, snapshot, args)
            finally:
                reset_current_session_key(token)

        try:
            # _run_in_executor_with_context, not a bare hop: the reviewer subagent is spawned from
            # the worker and inherits its context; a bare hop would run it under the launch home.
            result = await self._run_in_executor_with_context(_dispatch)
        except ValueError as exc:
            return str(exc)
        except Exception as exc:
            return t("gateway.review.start_failed", error=exc)
        from agent.review_engine import format_dispatch_note
        return format_dispatch_note(result, args)

    async def _handle_subgoal_command(self, event: MessageEvent) -> str:
        """Handle /subgoal (mirror of the CLI handler): extra criteria appended to the active goal
        mid-loop. They modify state read at the next turn boundary, so this is safe while the
        agent is running."""
        args = (event.get_command_args() or "").strip()
        mgr, _session_entry = await self._get_goal_manager_for_event(event)
        if mgr is None:
            return t("gateway.goal.unavailable")
        if not mgr.has_goal():
            return t("gateway.subgoal.no_goal")
        if not args:
            return f"{mgr.status_line()}\n{mgr.render_subgoals()}"
        tokens = args.split(None, 1)
        verb = tokens[0].lower()
        rest = tokens[1].strip() if len(tokens) > 1 else ""
        if verb == "remove":
            if not rest:
                return t("gateway.subgoal.usage_remove")
            try:
                idx = int(rest.split()[0])
            except ValueError:
                return t("gateway.subgoal.remove_not_int")
            removed, err = _mgr_call(
                "/subgoal remove", mgr.remove_subgoal, idx, errors=(IndexError, RuntimeError)
            )
            return err or t("gateway.subgoal.removed", index=idx, text=removed)
        if verb == "clear":
            prev, err = _mgr_call("/subgoal clear", mgr.clear_subgoals, errors=(RuntimeError,))
            if err:
                return err
            if not prev:
                return t("gateway.subgoal.none_to_clear")
            return t("gateway.subgoal.cleared_one" if prev == 1 else "gateway.subgoal.cleared_other", count=prev)
        text, err = _mgr_call("/subgoal", mgr.add_subgoal, args)
        if err:
            return err
        idx = len(mgr.state.subgoals) if mgr.state else 0
        return t("gateway.subgoal.added", index=idx, text=text)

    async def _handle_loop_command(self, event: MessageEvent) -> str:
        """Handle /loop — recurring in-session wakeups, via ``dispatch_loop_command`` (CLI mirror)."""
        try:
            from hermes_cli.loops import LoopManager, dispatch_loop_command, goal_blocks_loop_tick
        except Exception as exc:
            logger.debug("loops module unavailable: %s", exc)
            return t("gateway.loop.unavailable")

        # Warm the SessionDB cache off-loop: a cold cache drops the first /loop write while the
        # reply claims the loop was set (same class as the /goal false-ack fix).
        await self._warm_goals_session_db("loop manager")
        try:
            session_entry = await self.async_session_store.get_or_create_session(event.source)
        except Exception:
            session_entry = None
        sid = getattr(session_entry, "session_id", None) or ""
        if not sid:
            return t("gateway.loop.no_session")
        mgr = LoopManager(session_id=sid)

        # New loops capture the event's routing so the idle loop-wakeup watcher can inject ticks
        # here after a restart; best-effort, empty fields dropped. ``profile`` pins the wakeup to this
        # session's own bot under multiplex (the watcher must never fire it through the default bot).
        route: dict = {}
        try:
            src = event.source
            if src is not None:
                platform = getattr(src, "platform", "")
                route = {"platform": platform.value if hasattr(platform, "value") else str(platform or "")}
                for key in ("chat_id", "chat_type", "thread_id", "user_id", "user_name", "profile"):
                    route[key] = str(getattr(src, key, "") or "")
                route = {k: v for k, v in route.items() if v}
        except Exception:
            route = {}
        result = dispatch_loop_command(mgr, (event.get_command_args() or "").strip(), route=route)
        output = result.get("output") or ""
        if result.get("created") and _quiet_bool(lambda: goal_blocks_loop_tick(mgr.session_id)):
            output += t("gateway.loop.goal_defers_note")
        return output

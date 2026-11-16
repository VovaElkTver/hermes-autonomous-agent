"""Managed-worker turn loop (kanban excision 15.11: extracted from session_kanban).

The worker's chat turn binds the session workspace exactly as the in-process
runner does, then runs ordinary conversation turns. No board admission: the
kanban policy branch (kanban_json) was removed with the kanban subsystem.
"""
import contextlib
import json


@contextlib.contextmanager
def _worker_workspace(frame):
    """A managed worker's chat turn in its session's workspace, as the in-process turn runner binds it
    (#140665): the session id as the terminal task id, the route as the session key (persistent Docker
    keys the workspace container from it) and the frozen cwd as the session's mount. Yields the task
    id, or None for a frame with no session scope (nothing is bound then)."""
    session_id = (frame.get('scope') or {}).get('session_id')
    if not session_id:
        yield None
        return
    from gateway.session_authorities import served_profile_name
    from gateway.session_context import clear_session_vars, set_session_vars
    from gateway.session_policy import bind_turn_workspace, restore_policy
    from hermes_state_keys import profile_from_session_key
    policy = restore_policy(frame['policy'])
    route = frame.get('route') or session_id
    # The profile its in-process turns bind, so persistent Docker keys the container they and the
    # session's side agents use (#140665): a served secondary's name, which its routes carry
    # (``agent:<name>:``); none for the launch home, whatever its name (``hermes -p beta gateway``
    # keys ``agent:main:``, and only the default profile runs the multiplexer).
    profile = served_profile_name(frame.get('home'))
    tokens = set_session_vars(session_key=route, session_id=session_id,
                              profile=profile if profile_from_session_key(route) == profile else '')
    try:
        bind_turn_workspace(policy, session_id)
        yield session_id
    finally:
        clear_session_vars(tokens)


def run_worker_turns(agent, frame, history):
    from gateway.session_managed_worker import worker_turn_scope
    from gateway.session_surface import arm_surface_turn
    with worker_turn_scope(frame):
        # The agent consumes explicit one-turn attributes, not the ContextVar: arm them exactly as
        # the in-process runner does (voice route marker + surface note on the user message).
        arm_surface_turn(agent)
        with _worker_workspace(frame) as task_id:
            from gateway.session_display import worker_display_kwargs
            author = frame.get('turn_author')
            return agent.run_conversation(frame['text'], conversation_history=history, **worker_display_kwargs(),
                                          **({'task_id': task_id} if task_id else {}),
                                          **({'turn_author': author} if author is not None else {}))

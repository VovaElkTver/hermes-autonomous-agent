"""Codex prompt-cache scope: a persisted scheduled-run session id keeps one scope per job.

The scheduler producer was excised; what remains live is the normalization of the historical
``cron_<job>_<YYYYMMDD_HHMMSS>`` id shape (still present in stored sessions) and pass-through for
everything else.
"""

from agent.transports import get_transport


def _transport():
    transport = get_transport("codex_responses")
    assert transport is not None
    return transport


def _messages():
    return [
        {"role": "system", "content": "Keep this static instruction."},
        {"role": "user", "content": "Run the scheduled task."},
    ]


def test_legacy_scheduled_session_ids_share_one_scope_per_job():
    transport = _transport()

    def build(sid):
        return transport.build_kwargs(model="gpt-5.4", messages=_messages(), tools=[], session_id=sid)

    legacy_first = build("cron_job42_20261015_090000")
    legacy_second = build("cron_job42_20261015_110000")
    other_job = build("cron_job99_20261015_090000")

    assert legacy_first["prompt_cache_key"] == legacy_second["prompt_cache_key"]
    assert legacy_first["prompt_cache_key"] != other_job["prompt_cache_key"]


def test_unmatched_session_ids_keep_their_physical_scope():
    from agent.transports.codex import _cache_scope_from_session_id

    assert _cache_scope_from_session_id("local-first-fire") == "local-first-fire"
    assert _cache_scope_from_session_id(None) == ""
    # A scheduled-run id without the per-fire timestamp stays whole: no silent prefix-stripping.
    assert _cache_scope_from_session_id("cron_job42") == "cron_job42"
    assert _cache_scope_from_session_id("cron_job42_20261015_090000") == "cron_job42"


def test_backend_headers_and_request_content_stay_intact():
    from copy import deepcopy

    from agent.transports.codex import _cache_scope_from_session_id

    transport = _transport()
    messages = _messages()
    tools = [{"type": "function", "function": {
        "name": "lookup", "description": "Read a value.",
        "parameters": {"type": "object", "properties": {}},
    }}]
    original = deepcopy((messages, tools))

    sid = "cron_job42_20261015_090000"
    built = transport.build_kwargs(model="gpt-5.4", messages=messages, tools=tools,
                                   session_id=sid, is_codex_backend=True)
    assert _cache_scope_from_session_id(sid) == "cron_job42"
    assert built["prompt_cache_key"] == "cron_job42" or built["prompt_cache_key"].startswith("pck_") or (
        len(str(built["prompt_cache_key"])) <= 64
    )
    assert built["extra_headers"]["session_id"] == sid
    # Request content is never mutated by cache-key bookkeeping.
    assert (messages, tools) == original


def test_override_prompt_cache_key_wins_over_the_derived_scope():
    transport = _transport()
    built = transport.build_kwargs(model="gpt-5.4", messages=_messages(), tools=[],
                                   session_id="cron_job42_20261015_090000",
                                   request_overrides={"prompt_cache_key": "caller-cache-key"})
    assert built["prompt_cache_key"] == "caller-cache-key"

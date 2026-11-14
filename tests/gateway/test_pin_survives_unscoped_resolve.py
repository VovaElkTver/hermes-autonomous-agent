"""rtx5090 П8 (14.11): a session's /model pin must survive resolution passes that run with no
profile scope bound — boot rehydration and background passes on the multiplexed gateway.

The incident: OPENAI_* credentials live ONLY in the unit's Environment= (never in .env); after a
drain-safe reload the persisted override was re-resolved unscoped, the provider ladder's leak gate
raised UnscopedSecretError (swallowed at DEBUG by the rehydrator), and every later turn rode the
default route while the session believed it was pinned to glm-libert.
"""
import types

import pytest

# Import the gateway chain at collection time (before the real-home I/O guard activates):
# hermes_bootstrap probes ``<checkout parent>/manifest.json``, and worktrees living under
# ~/.hermes/cache make that probe cross the guarded boundary otherwise.
import gateway.run as gr
from gateway.run_agent_cache import GatewayAgentCacheMixin
import tui_gateway.launch_profile_policy as lpp
import hermes_constants

import agent.secret_scope as ss
from agent.secret_scope import UnscopedSecretError, set_secret_scope, reset_secret_scope


@pytest.fixture(autouse=True)
def _no_multiplex_leftover():
    ss.set_multiplex_active(False)
    yield
    ss.set_multiplex_active(False)


# --- leak gate fails soft, never leaks ---------------------------------------------------------

def test_leak_gate_reads_are_fail_soft_when_unscoped(monkeypatch):
    from hermes_cli import runtime_provider as rp

    ss.set_multiplex_active(True)  # no scope bound: the background-pass context
    # Before the fix this raised UnscopedSecretError out of the ladder.
    assert rp._host_gated_env_key_candidates("http://192.168.9.100:8012/v1", ollama=False) == ["", "", ""]


def test_leak_gate_still_honors_host_pairing_when_scoped(monkeypatch):
    from hermes_cli import runtime_provider as rp

    ss.set_multiplex_active(True)
    token = set_secret_scope({
        "OPENAI_API_KEY": "***",
        "OPENAI_BASE_URL": "http://127.0.0.1:1/v1",  # paired proxy URL, loopback host
    })
    try:
        # Exact pairing (base_url == OPENAI_BASE_URL) delivers the env key.
        cands = rp._host_gated_env_key_candidates("http://127.0.0.1:1/v1", ollama=False)
        assert cands[0] == "***"
        # An unrelated host must NOT receive the OPENAI key (GHSA-76xc-57q6-vm5m semantics).
        cands = rp._host_gated_env_key_candidates("http://example.com:9/v1", ollama=False)
        assert cands[0] == ""
    finally:
        reset_secret_scope(token)


# --- boot rehydration resolves under the launch body -------------------------------------------

def test_rehydrate_resolves_env_only_credential_without_turn_scope(monkeypatch, tmp_path):
    monkeypatch.setattr(hermes_constants, "get_process_hermes_home", lambda: tmp_path)

    ss.set_multiplex_active(True)  # boot pass: multiplex on, no per-turn scope ever bound

    def fake_resolver(provider, target_model=None):
        # Mimics the custom-provider ladder: consults the leak gate, which raises unscoped.
        base = ss.get_secret_str("OPENAI_BASE_URL", "")
        key = ss.get_secret_str("OPENAI_API_KEY", "")
        if not key:
            raise AssertionError("no credential resolved")
        return {"api_key": key, "api_mode": "chat_completions", "credential_pool": None,
                "requested_provider": provider, "max_tokens": None, "base_url": base,
                "request_overrides": {}, "capabilities": {}}

    monkeypatch.setattr(gr, "_resolve_runtime_agent_kwargs_for_provider", fake_resolver)
    monkeypatch.setattr(lpp, "launch_secret_scope",
                        lambda home: {"OPENAI_BASE_URL": "http://192.168.9.100:8012/v1",
                                      "OPENAI_API_KEY": "***"})

    conv = types.SimpleNamespace(model_override=None)
    state = types.SimpleNamespace(conversation=conv)
    obj = types.SimpleNamespace(
        session_store=types.SimpleNamespace(get_model_override=lambda k: {
            "model": "glm-5.3-flash-libert", "provider": "glm-libert",
            "base_url": "http://192.168.9.100:8012/v1"}),
        _session_model_override=lambda k: None,
        _session_state=lambda k: state,
    )
    GatewayAgentCacheMixin._rehydrate_session_model_override(obj, "agent:main:matrix:dm:x")
    assert conv.model_override["api_key"] == "***", (
        "persisted pin re-resolved credential-less: an unscoped rehydrate silently demoted "
        "the session to the default route (П8)")

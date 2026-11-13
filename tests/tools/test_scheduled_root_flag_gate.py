"""Root-flag gating of scheduled jobs (Вл 09.11): «все наши придумки под отдельным корневым
конфиг флагом». autonomous_mode=true (fork default) ⇒ donor surfaces run on their own
posture. autonomous_mode=false → donor upstream parity — except the scheduled-jobs package,
which was excised everywhere."""

from __future__ import annotations

import pytest


@pytest.fixture()
def fork_cfg(monkeypatch):
    holder = {"map": {}}
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: holder["map"], raising=False)
    return holder["map"]


def test_cron_gate_mirrors_root_flag(fork_cfg) -> None:
    from tools.fork_mode import scheduled_runtime_enabled
    assert scheduled_runtime_enabled() is False  # fork default: autonomous ON → jobs OFF
    fork_cfg["autonomous_mode"] = False
    assert scheduled_runtime_enabled() is True   # donor parity posture


def test_api_server_jobs_surface_is_gone() -> None:
    """The REST surface died with the package: not even fail-closed stubs remain."""
    import gateway.platforms.api_server as api

    for gone in ("_CRON_AVAILABLE", "_cron_list", "_cron_get", "_cron_create", "_cron_update",
                 "_cron_remove", "_cron_pause", "_cron_resume", "_cron_trigger",
                 "_scan_cron_prompt", "_notify_cron_provider_jobs_changed",
                 "_CronSchedulerRegistrationError"):
        assert not hasattr(api, gone), f"api_server still carries {gone!r}"
    for method in ("_cron_request_guard", "_cron_error_response", "_validate_cron_prompt",
                   "_handle_list_jobs", "_handle_create_job", "_handle_get_job",
                   "_handle_update_job", "_handle_delete_job", "_handle_pause_job",
                   "_handle_resume_job", "_handle_run_job", "_cron_origin_from_request"):
        assert not hasattr(api.APIServerAdapter, method), f"APIServerAdapter still has {method!r}"


def test_webhook_legacy_trigger_route_fails_validation(monkeypatch, tmp_path) -> None:
    """A subscription still declaring the legacy trigger key fails loudly at load time."""
    from gateway.config import PlatformConfig
    from gateway.platforms.webhook import WebhookAdapter

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={"host": "127.0.0.1", "port": 0}))
    stale_route = {"secret": "s", "prompt": "x", "cron_job": "job1"}
    with pytest.raises(ValueError, match="excised feature"):
        adapter._validate_route("legacy", stale_route)

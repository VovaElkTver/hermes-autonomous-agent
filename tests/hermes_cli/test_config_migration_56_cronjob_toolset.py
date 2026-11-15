"""Migration 55→56: the retired cronjob toolset is stripped from saved lists.

The 10.11 cron excision deleted the "cronjob" toolset, the hermes-cron bundle and the
cronjob_manage deferral entry; picker saves and backfills can still carry those names.
Unknown toolset names resolve to ∅ at runtime (this is hygiene, mirroring the migration-39
"bfl" precedent), but stale references must not survive an update either. User entries
that merely *contain* "cron" (a custom MCP toolset named cron_report) are untouched.
Driven through ``run_migrations`` against a temp home.
"""

import os
from unittest.mock import patch

import hermes_yaml as yaml


def _seed(tmp_path, version=55):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({
        "_config_version": version,
        "platform_toolsets": {"cli": ["file", "terminal", "cronjob"], "matrix": ["file", "cronjob"]},
        "known_builtin_toolsets": {"cli": ["file", "web", "cronjob"], "matrix": ["file"]},
        "agent": {"disabled_toolsets": ["cronjob", "homebrew"]},
        "tools": {"tool_search": {"enabled": "auto", "defer": ["session_search", "cronjob_manage"]}},
    }), encoding="utf-8")
    return tmp_path


def _migrate(tmp_path):
    from hermes_cli.config_migrations import run_migrations
    with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}, clear=False):
        run_migrations(55, {"env_added": [], "config_added": [], "warnings": []}, quiet=True)
    return yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))


def test_retired_cronjob_names_are_stripped_everywhere(tmp_path):
    cfg = _migrate(_seed(tmp_path / "seeded"))
    assert cfg["platform_toolsets"]["cli"] == ["file", "terminal"]
    assert cfg["platform_toolsets"]["matrix"] == ["file"]
    assert cfg["known_builtin_toolsets"]["cli"] == ["file", "web"]
    assert cfg["agent"]["disabled_toolsets"] == ["homebrew"]
    assert cfg["tools"]["tool_search"]["defer"] == ["session_search"]


def test_clean_config_untouched_and_idempotent(tmp_path):
    clean = _seed(tmp_path / "clean")
    _migrate(clean)
    before = (clean / "config.yaml").read_text(encoding="utf-8")
    with patch.dict(os.environ, {"HERMES_HOME": str(clean)}, clear=False):
        from hermes_cli.config_migrations import run_migrations
        run_migrations(56, {"env_added": [], "config_added": [], "warnings": []}, quiet=True)
    assert (clean / "config.yaml").read_text(encoding="utf-8") == before


def test_names_that_merely_contain_cron_survive(tmp_path):
    home = _seed(tmp_path / "custom")
    cfg = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    cfg["platform_toolsets"]["cli"].append("cron_report")
    cfg["agent"]["disabled_toolsets"].append("cron_report")
    (home / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    out = _migrate(home)
    assert "cron_report" in out["platform_toolsets"]["cli"]
    assert "cron_report" in out["agent"]["disabled_toolsets"]

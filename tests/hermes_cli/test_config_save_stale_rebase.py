"""Stale full-tree ``save_config`` must not delete branches it never saw.

Regression for the 17.11 incident: a process that had loaded the config before the
morning ``agent.verify_on_stop`` activation rewrote the whole file in the evening and
silently dropped the activation — the flag was OFF for four days without any decision
being recorded. The throat is ``rebase_for_save``: live branches absent from the
process's last loaded/saved base are concurrent additions and must survive; branches
the caller saw and dropped stay dropped (deletion intent is honored).
"""
from pathlib import Path


from hermes_cli import config as C
from hermes_cli.config_worker_save import rebase_for_save


def _rebase(tmp_path, monkeypatch, *, live, desired, base):
    assert C._worker_config_snapshot() is None, "test assumes non-frozen process"
    path = tmp_path / "config.yaml"
    monkeypatch.setattr(C, "get_config_path", lambda: path)
    cache = {str(path): base} if base is not None else {}
    monkeypatch.setattr(C, "_LAST_EXPANDED_CONFIG_BY_PATH", cache)
    return rebase_for_save(desired, live, False)


def test_concurrent_top_level_section_survives_stale_save(tmp_path, monkeypatch):
    live = {"model": {"default": "m"}, "agent": {"verify_on_stop": "auto"}}
    desired = {"model": {"default": "m"}, "managed_refine": False}
    base = {"model": {"default": "m"}}  # caller loaded before the activation
    out = _rebase(tmp_path, monkeypatch, live=live, desired=desired, base=base)
    assert out.get("agent") == {"verify_on_stop": "auto"}
    assert out["managed_refine"] is False  # the caller's own edit lands
    assert live["agent"] is not out["agent"], "deep copy, caller cannot alias disk"


def test_concurrent_key_inside_existing_section_survives(tmp_path, monkeypatch):
    live = {"agent": {"disabled_toolsets": [], "verify_on_stop": "auto"}}
    desired = {"agent": {"disabled_toolsets": []}}
    base = {"agent": {"disabled_toolsets": []}}
    out = _rebase(tmp_path, monkeypatch, live=live, desired=desired, base=base)
    assert out["agent"]["verify_on_stop"] == "auto"


def test_seen_then_deleted_branch_stays_deleted(tmp_path, monkeypatch):
    live = {"agent": {"verify_on_stop": "auto"}, "model": {"default": "m"}}
    desired = {"model": {"default": "m"}}  # caller saw agent and removed it
    base = {"agent": {"verify_on_stop": "auto"}, "model": {"default": "m"}}
    out = _rebase(tmp_path, monkeypatch, live=live, desired=desired, base=base)
    assert "agent" not in out


def test_seen_then_removed_key_stays_removed(tmp_path, monkeypatch):
    live = {"agent": {"verify_on_stop": "auto", "other": 1}}
    desired = {"agent": {"other": 1}}
    base = {"agent": {"verify_on_stop": "auto", "other": 1}}
    out = _rebase(tmp_path, monkeypatch, live=live, desired=desired, base=base)
    assert "verify_on_stop" not in out["agent"]


def test_caller_scalar_and_list_wins(tmp_path, monkeypatch):
    live = {"command_allowlist": ["a", "b"], "tts": {"provider": "piper"}}
    desired = {"command_allowlist": ["a"]}  # caller saw both, removed b: wholesale replace
    base = {"command_allowlist": ["a", "b"]}
    out = _rebase(tmp_path, monkeypatch, live=live, desired=desired, base=base)
    assert out["command_allowlist"] == ["a"]
    assert out["tts"] == {"provider": "piper"}  # never-seen section preserved


def test_no_base_provenance_conservatively_preserves(tmp_path, monkeypatch):
    # A process that saved without ever loading cannot claim deletion intent.
    live = {"agent": {"verify_on_stop": "auto"}}
    desired = {"model": {"default": "m"}}
    out = _rebase(tmp_path, monkeypatch, live=live, desired=desired, base=None)
    assert out.get("agent") == {"verify_on_stop": "auto"}


def test_fail_open_on_internal_error(tmp_path, monkeypatch):
    def boom():
        raise RuntimeError("path resolution died")

    monkeypatch.setattr(C, "get_config_path", boom)
    live = {"agent": {"verify_on_stop": "auto"}}
    desired = {"model": {"default": "m"}}
    out = rebase_for_save(desired, live, False)
    assert out == {"model": {"default": "m"}}


def test_merge_existing_and_frozen_paths_untouched(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    monkeypatch.setattr(C, "get_config_path", lambda: path)
    live = {"a": {"x": 1}}
    desired = {"a": {"y": 2}}
    out = rebase_for_save(desired, live, True)
    assert out["a"] == {"x": 1, "y": 2}  # _merge_partial_save semantics unchanged

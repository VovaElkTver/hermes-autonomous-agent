"""Deafness contour R4 L2: fork-canonical gateway-watchdog delivery to the live home.

``step_sync_gateway_watchdog`` (HOME_STEPS) copies the versioned canon from the
fork's ``scripts/`` into ``$HERMES_HOME/scripts/`` where the node's systemd user
timer executes it — so the muscle evolves WITH the fork instead of freezing.
"""
import hermes_constants
import pm.paths
from hermes_cli import post_update


def _wire(tmp_path, monkeypatch, source_content: str | None):
    repo, home = tmp_path / "repo", tmp_path / "home"
    (repo / "scripts").mkdir(parents=True)
    if source_content is not None:
        (repo / "scripts" / "gateway-watchdog.py").write_text(source_content)
    monkeypatch.setattr(pm.paths, "install_root", lambda: repo)
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: home)
    return home


def test_sync_watchdog_copies_when_content_differs(tmp_path, monkeypatch):
    home = _wire(tmp_path, monkeypatch, "canon-v2")
    (home / "scripts").mkdir(parents=True)
    (home / "scripts" / "gateway-watchdog.py").write_text("frozen-node-copy")

    res = post_update.step_sync_gateway_watchdog()

    assert res == {"ok": True, "synced": str(home / "scripts" / "gateway-watchdog.py")}
    assert (home / "scripts" / "gateway-watchdog.py").read_text() == "canon-v2"


def test_sync_watchdog_is_idempotent_when_current(tmp_path, monkeypatch):
    home = _wire(tmp_path, monkeypatch, "canon")
    (home / "scripts").mkdir(parents=True)
    dest = home / "scripts" / "gateway-watchdog.py"
    dest.write_text("canon")

    res = post_update.step_sync_gateway_watchdog()

    assert res == {"ok": True, "skipped": "current"}
    assert dest.read_text() == "canon"


def test_sync_watchdog_skips_when_no_source(tmp_path, monkeypatch):
    home = _wire(tmp_path, monkeypatch, None)

    res = post_update.step_sync_gateway_watchdog()

    assert res == {"ok": True, "skipped": "no-source"}
    assert not (home / "scripts" / "gateway-watchdog.py").exists()

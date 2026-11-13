"""Acceptance suite for plugins/config-protection (ТЗ 10.11, приёмка а–е)."""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PLUGIN_DIR = REPO / "plugins" / "config-protection"


def _load():
    spec = importlib.util.spec_from_file_location(
        "config_protection_plugin", PLUGIN_DIR / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def cp(tmp_path, monkeypatch):
    """Plugin module + sandboxed HERMES_HOME + protected_files pointed at sandbox."""
    mod = _load()
    fake_home = tmp_path / "hermes-home"
    fake_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(fake_home))
    # fresh fake config.yaml + topics.yaml + scheduled/jobs.json inside the sandbox
    (fake_home / "cron").mkdir()
    (fake_home / "config.yaml").write_text("gate: true\n")
    (fake_home / "topics.yaml").write_text("t: 1\n")
    (fake_home / "cron" / "jobs.json").write_text("{}")
    cfg = {"protected_files": [str(fake_home / "config.yaml"),
                               str(fake_home / "topics.yaml"),
                               str(fake_home / "cron" / "jobs.json")]}
    import hermes_cli.config as hcfg
    monkeypatch.setattr(hcfg, "load_config", lambda: cfg)
    return mod, cfg


def call(mod, tool, args):
    return mod.pre_tool_call(tool_name=tool, args=args)


def test_a_create_new_allowed(cp):
    mod, _cfg = cp
    new_cfg = Path(os.environ["HERMES_HOME"]) / "config2.yaml"
    assert call(mod, "write_file", {"path": str(new_cfg)}) is None      # new file passes


def test_b_edit_existing_blocked(cp):
    mod, _cfg = cp
    home = Path(os.environ["HERMES_HOME"])
    r = call(mod, "write_file", {"path": str(home / "config.yaml")})
    assert r and r["action"] == "block"
    r2 = call(mod, "patch", {"path": str(home / "cron" / "jobs.json"),
                             "old_string": "{}", "new_string":"{"})
    assert r2 and r2["action"] == "block"


def test_c_dangling_symlink_caught(cp, tmp_path):
    mod, _cfg = cp
    home = Path(os.environ["HERMES_HOME"])
    real = home / "topics.yaml"
    real.unlink()
    real.symlink_to(tmp_path / "no-such-target")   # now it dangles
    r = call(mod, "write_file", {"path": str(real)})
    assert r and r["action"] == "block"     # lstat sees it; exists() would not


def test_d_case_variant_no_slip(cp):
    mod, _cfg = cp
    home = Path(os.environ["HERMES_HOME"])
    odd = home / "CONFIG.YAML"
    odd.write_text("x\n")
    r = call(mod, "execute_code", {"code": f"open('{odd}', 'w').write('t')"})
    assert r and r["action"] == "block"


def test_e_hook_crash_fail_open(cp, monkeypatch, capsys):
    mod, _cfg = cp

    def boom():
        raise RuntimeError("wedged")
    monkeypatch.setattr(mod, "_protected_globs", boom)
    capsys.readouterr()                       # drain prior output
    r = call(mod, "write_file", {"path": "/etc/passwd"})   # anything; must allow
    assert r is None                          # fail-open: tool call unaffected
    err = capsys.readouterr().err
    assert "[config-protection]" in err       # swallowed error diagnosed on stderr


def test_f_globs_reread_each_call(cp, monkeypatch, tmp_path):
    """Config key change is live without restart."""
    mod, cfg = cp
    target = tmp_path / "some-guard.toml"
    target.write_text("a=1\n")
    cfg["protected_files"] = [str(target)]
    assert call(mod, "write_file", {"path": str(target)})["action"] == "block"

    # now drop the key → same file no longer protected, no restart in between
    cfg.pop("protected_files")
    assert call(mod, "write_file", {"path": str(target)}) is None


def test_reads_pass(cp):
    mod, _cfg = cp
    home = Path(os.environ["HERMES_HOME"])
    assert call(mod, "read_file", {"path": str(home / "config.yaml")}) is None


def test_register_wiring(cp):
    mod, _cfg = cp

    class Ctx:
        hooks = []

        def register_hook(self, event, fn):
            self.hooks.append((event, fn))

    ctx = Ctx()
    mod.register(ctx)
    assert ctx.hooks and ctx.hooks[0][0] == "pre_tool_call"

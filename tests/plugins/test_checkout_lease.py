"""Fork-guardrails v0.5.2 checkout_lease: one checkout, one session (ToDo §4).

Real temp git repos; two simulated sessions (sess-A owns the checkout, sess-B
is the foreign track). Covers: acquire, owner pass-through + renewal, foreign
block (git verb AND file write), stale-clean takeover, stale-dirty refusal,
read-only pass, log-only inertness."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest


def _load_plugin(home: Path):
    plugins_root = home / "plugins"
    plugins_root.mkdir(parents=True, exist_ok=True)
    if "plugins" not in sys.modules:
        pkg = types.ModuleType("plugins")
        pkg.__path__ = [str(plugins_root)]
        sys.modules["plugins"] = pkg
    src = Path(__file__).resolve().parents[2] / "plugins" / "fork-guardrails" / "__init__.py"
    for name in [k for k in sys.modules if k.startswith("plugins.fork_guardrails_test_")]:
        del sys.modules[name]
    modname = f"plugins.fork_guardrails_test_{abs(hash(str(home))) % 10**8}"
    spec = importlib.util.spec_from_file_location(modname, src)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def home(tmp_path, monkeypatch) -> Path:
    h = tmp_path / "hermes"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    return h


class _Ctx:
    def __init__(self, settings):
        self.settings = settings
        self.registered = {}

    def register_hook(self, name, cb):
        self.registered.setdefault(name, []).append(cb)

    def get_config(self, key, default=None):
        return self.settings.get(key, default)


def _mk_ctx(home: Path, log_only: bool) -> dict:
    # _settings() reads FLAT keys via ctx.get_config("enabled"/"audit_log"/...)
    return {"enabled": True, "log_only": log_only,
            "audit_log": str(home / "fork-guardrails/audit.jsonl")}


@pytest.fixture()
def plugin(home: Path):
    (home / "fork-guardrails").mkdir(parents=True, exist_ok=True)
    mod = _load_plugin(home)
    mod.register(_Ctx(_mk_ctx(home, False)))
    return mod


@pytest.fixture()
def plugin_logonly(home: Path):
    (home / "fork-guardrails").mkdir(parents=True, exist_ok=True)
    mod = _load_plugin(home)
    mod.register(_Ctx(_mk_ctx(home, True)))
    return mod


@pytest.fixture()
def repo(tmp_path_factory) -> Path:
    r = tmp_path_factory.mktemp("leased-repo")
    subprocess.run(["git", "init", "-q", str(r)], check=True)
    subprocess.run(["git", "-C", str(r), "config", "user.email", "t@t"], check=True)
    (r / "f.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(r), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(r), "commit", "-qm", "base"], check=True)
    return r


def _decide(mod, tool, args, sid):
    return mod.pre_tool_call(tool_name=tool, args=args, session_id=sid, turn_id="t1")


def _touch_clean(mod, repo: Path, sid: str) -> None:
    """Owner acquires the lease and leaves the tree clean (add+commit)."""
    assert _decide(mod, "terminal", {"command": f"git -C {repo} add -A"}, sid) is None
    assert _decide(mod, "terminal", {"command": f"git -C {repo} commit -m x"}, sid) is None


def test_acquire_on_first_mutating_op(plugin, repo):
    assert _decide(plugin, "terminal", {"command": f"git -C {repo} add -A"}, "sess-A") is None
    s = plugin._settings()
    lease = plugin._read_lease(plugin._lease_file(s, str(repo)))
    assert lease and lease["session_id"] == "sess-A"


def test_foreign_git_verb_blocked(plugin, repo):
    _touch_clean(plugin, repo, "sess-A")
    out = _decide(plugin, "terminal", {"command": f"git -C {repo} commit -m y"}, "sess-B")
    assert out["action"] == "block"
    assert "sess-A" in out["message"] and "worktree" in out["message"]


def test_foreign_file_write_blocked_owner_allowed(plugin, repo):
    _touch_clean(plugin, repo, "sess-A")
    victim = str(repo / "f.txt")
    out = _decide(plugin, "patch", {"path": victim, "old_string": "base", "new_string": "B"}, "sess-B")
    assert out["action"] == "block"
    assert _decide(plugin, "patch", {"path": victim, "old_string": "base", "new_string": "A"},
                   "sess-A") is None


def test_reads_and_outside_pass(plugin, repo):
    _touch_clean(plugin, repo, "sess-A")
    assert _decide(plugin, "terminal", {"command": f"git -C {repo} status"}, "sess-B") is None
    assert _decide(plugin, "write_file", {"path": "/tmp/outside-lease.py", "content": "x"},
                   "sess-B") is None


def test_stale_clean_takeover(plugin, repo):
    _touch_clean(plugin, repo, "sess-A")
    s = plugin._settings()
    lf = plugin._lease_file(s, str(repo))
    data = json.loads(Path(lf).read_text())
    data["ts"] = time.time() - 3600
    Path(lf).write_text(json.dumps(data))
    assert _decide(plugin, "terminal", {"command": f"git -C {repo} add -A"}, "sess-B") is None
    lease = plugin._read_lease(lf)
    assert lease["session_id"] == "sess-B" and lease["took_over_from"] == "sess-A"


def test_stale_dirty_not_stealable(plugin, repo):
    assert _decide(plugin, "terminal", {"command": f"git -C {repo} add -A"}, "sess-A") is None
    (repo / "f.txt").write_text("dirty\n", encoding="utf-8")  # owner left the tree dirty
    s = plugin._settings()
    lf = plugin._lease_file(s, str(repo))
    data = json.loads(Path(lf).read_text())
    data["ts"] = time.time() - 3600
    Path(lf).write_text(json.dumps(data))
    out = _decide(plugin, "terminal", {"command": f"git -C {repo} commit -m y"}, "sess-B")
    assert out["action"] == "block"
    assert "dirty" in out["message"]
    assert plugin._read_lease(lf)["session_id"] == "sess-A"  # lease untouched


def test_log_only_inert(plugin_logonly, repo, home):
    assert _decide(plugin_logonly, "terminal", {"command": f"git -C {repo} add -A"}, "sess-A") is None
    s = plugin_logonly._settings()
    assert plugin_logonly._read_lease(plugin_logonly._lease_file(s, str(repo))) is None  # no lease minted
    out = _decide(plugin_logonly, "terminal", {"command": f"git -C {repo} commit -m x"}, "sess-B")
    assert out is None  # log-only never blocks
    audit = Path(home / "fork-guardrails/audit.jsonl")
    lines = audit.read_text().splitlines() if audit.exists() else []
    assert not any("checkout" in l or "lease" in l for l in lines)  # shadow mode minted nothing


def test_finalize_releases(plugin, repo):
    _touch_clean(plugin, repo, "sess-A")
    s = plugin._settings()
    assert plugin._read_lease(plugin._lease_file(s, str(repo)))["session_id"] == "sess-A"
    plugin.on_session_finalize(session_id="sess-A")
    assert plugin._read_lease(plugin._lease_file(s, str(repo))) is None
    # B may now acquire
    assert _decide(plugin, "terminal", {"command": f"git -C {repo} add -A"}, "sess-B") is None

"""Toggles + log-only mode for fork-guardrails v0.3.0 (plugins/fork-guardrails/).

DoFirst §3.2 D0: the 27.10 crash-loop recovery could only disable the plugin
whole, because there was no per-feature surface — "all or nothing". These
tests pin the new settings contract:

- zero settings → enforcing defaults, byte-compatible with 0.2.x;
- ``enabled`` / ``features.<name>`` toggles flip individual guards without
  a gateway restart (settings are hot-read);
- ``log_only`` downgrades every verdict to allow + an audit line
  (would_block / would_approve / internal_error) — zero impact on behavior;
- enforced verdicts also write an audit line (the metric surface, М.4);
- a ctx without get_config degrades to the enforcing defaults.

Same standalone-load convention as test_fork_guardrails.py. Under pytest the
home_io_guard keeps any default-path audit write hermetic (it trips instead
of touching the real home), so audit assertions use an explicit tmp_path via
the settings surface.
"""

import importlib.util
import json
import sys
import types

import pytest

ROOT = None  # set below


def _load_plugin():
    import pathlib
    import threading
    root = pathlib.Path(__file__).resolve().parents[2]
    reg = types.ModuleType("tools.delegate_tool_registry")
    reg._active_subagents, reg._active_subagents_lock = {}, threading.Lock()
    sys.modules.setdefault("tools", types.ModuleType("tools"))
    sys.modules["tools.delegate_tool_registry"] = reg
    plugin_dir = root / "plugins" / "fork-guardrails"
    spec = importlib.util.spec_from_file_location(
        "fork_guardrails_toggles_under_test", plugin_dir / "__init__.py",
        submodule_search_locations=[str(plugin_dir)])
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "fork_guardrails_toggles_under_test"
    mod.__path__ = [str(plugin_dir)]
    sys.modules["fork_guardrails_toggles_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


fg = _load_plugin()


class SettingsCtx:
    """PluginContext stand-in: register_hook + hot get_config over a mutable dict."""

    def __init__(self, settings: dict | None = None):
        self.settings = settings if settings is not None else {}
        self.registered: dict = {}

    def register_hook(self, name, cb):
        self.registered.setdefault(name, []).append(cb)

    def get_config(self, key, default=None):
        return self.settings.get(key, default)


class BareCtx:
    """No get_config at all — the 0.2.x-era fixture shape."""

    def __init__(self):
        self.registered = {}

    def register_hook(self, name, cb):
        self.registered.setdefault(name, []).append(cb)


@pytest.fixture()
def make_environ():
    """Fresh state + registered hooks per test; returns (ctx_factory, hooks)."""
    fg._failures.clear()
    fg._delegation_ok.clear()

    def factory(settings=None, bare=False):
        ctx = BareCtx() if bare else SettingsCtx(settings)
        fg.register(ctx)
        return ctx

    return factory


def directive(ctx, tool, args, session_id="s1", turn_id="t1"):
    """Mirror _get_pre_tool_call_directive_details precedence: block wins, else first approve."""
    out = []
    for cb in ctx.registered.get("pre_tool_call", []):
        out.append(cb(tool_name=tool, args=args, task_id="k", session_id=session_id,
                     tool_call_id="c", turn_id=turn_id, api_request_id="a", middleware_trace=[]))
    ap = None
    for r in out:
        if not isinstance(r, dict):
            continue
        if r.get("action") == "block" and r.get("message"):
            return ("block", None)
        if r.get("action") == "approve" and ap is None:
            ap = ("approve", r.get("rule_key"))
    return ap or (None, None)


def audit_lines(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]
    except FileNotFoundError:
        return []


def test_zero_config_is_enforcing_default(make_environ):
    """No settings (empty dict, like a plugin with a zero entry) → 0.2.x behavior."""
    ctx = make_environ({})
    assert directive(ctx, "terminal", {"command": "git push origin feature"})[0] == "approve"
    assert directive(ctx, "terminal", {"command": "rm -rf build/"})[0] == "approve"
    assert directive(ctx, "terminal", {"command": "git push -f origin main"})[0] == "block"
    assert directive(ctx, "delegate_task", {"goal": "x"})[0] == "block"
    date_ctx = ctx.registered["pre_llm_call"][0](session_id="s1", user_message="hi",
                                                  conversation_history=[], is_first_turn=True,
                                                  model="m", platform="cli")
    assert "Current date:" in date_ctx["context"]


def test_ctx_without_get_config_degrades_to_defaults(make_environ):
    ctx = make_environ(bare=True)
    assert directive(ctx, "terminal", {"command": "git push origin x"})[0] == "approve"
    assert directive(ctx, "terminal", {"command": "git push -f origin main"})[0] == "block"


def test_master_disabled_passes_everything(make_environ):
    ctx = make_environ({"enabled": False})
    assert directive(ctx, "terminal", {"command": "git push origin x"}) == (None, None)
    assert directive(ctx, "terminal", {"command": "git push -f origin main"}) == (None, None)
    assert directive(ctx, "terminal", {"command": "rm -rf build/"}) == (None, None)
    assert directive(ctx, "delegate_task", {"goal": "x"}) == (None, None)
    date_ctx = ctx.registered["pre_llm_call"][0](session_id="s1", user_message="hi",
                                                  conversation_history=[], is_first_turn=True,
                                                  model="m", platform="cli")
    assert date_ctx == {}


@pytest.mark.parametrize("feature, probe", [
    ("outbound_approve", lambda c: directive(c, "terminal", {"command": "git push origin x"}) == (None, None)),
    # force_push_block off: -f to main is no longer a block — it degrades to the
    # outbound git-push approve (0.2.x semantics: --force-with-lease was always approve)
    ("force_push_block", lambda c: directive(c, "terminal", {"command": "git push -f origin main"}) == ("approve", "fork-guardrails:git-push")),
    ("delegation_gate", lambda c: directive(c, "delegate_task", {"goal": "x"}) == (None, None)),
])
def test_feature_off_single_guard_flaps(make_environ, feature, probe):
    settings = {"features": {f: (f != feature) for f in fg._FEATURES}}
    ctx = make_environ(settings)
    assert probe(ctx), feature


def test_retry_guard_off_ignores_failure_streak(make_environ):
    ctx = make_environ({"features": {"retry_guard": False}})
    for _ in range(3):
        for cb in ctx.registered.get("post_tool_call", []):
            cb(tool_name="terminal", args={"command": "make"}, result="{}", status="error",
               task_id="k", duration_ms=1, session_id="s1", turn_id="t1")
    assert directive(ctx, "terminal", {"command": "make"}) == (None, None)


def test_date_context_off(make_environ):
    ctx = make_environ({"features": {"date_context": False}})
    out = ctx.registered["pre_llm_call"][0](session_id="s1", user_message="hi",
                                            conversation_history=[], is_first_turn=True,
                                            model="m", platform="cli")
    assert out == {}


def test_log_only_downgrades_and_audits(make_environ, tmp_path):
    audit = str(tmp_path / "audit.jsonl")
    ctx = make_environ({"log_only": True, "audit_log": audit})
    assert directive(ctx, "terminal", {"command": "rm -rf build/"}) == (None, None)
    assert directive(ctx, "terminal", {"command": "git push -f origin main"}) == (None, None)
    lines = audit_lines(audit)
    kinds = [l["kind"] for l in lines]
    assert kinds.count("would_approve") == 1
    assert kinds.count("would_block") == 1
    assert lines[0]["rule_key"] == "fork-guardrails:rm-recursive"
    assert lines[0]["session_id"] == "s1"


def test_enforced_verdict_audits(make_environ, tmp_path):
    audit = str(tmp_path / "audit.jsonl")
    ctx = make_environ({"audit_log": audit})
    assert directive(ctx, "terminal", {"command": "rm -rf build/"})[0] == "approve"
    lines = audit_lines(audit)
    assert len(lines) == 1
    assert lines[0]["kind"] == "approve"
    assert lines[0]["rule_key"] == "fork-guardrails:rm-recursive"


def test_log_only_internal_error_allows(make_environ, tmp_path, monkeypatch):
    audit = str(tmp_path / "audit.jsonl")
    ctx = make_environ({"log_only": True, "audit_log": audit})
    monkeypatch.setattr(fg, "_failures", None)  # force an exception inside _decide
    assert directive(ctx, "terminal", {"command": "ls"}) == (None, None)
    assert [l["kind"] for l in audit_lines(audit)] == ["internal_error"]


def test_enforced_internal_error_blocks(make_environ, monkeypatch):
    ctx = make_environ({})
    monkeypatch.setattr(fg, "_failures", None)
    assert directive(ctx, "terminal", {"command": "ls"})[0] == "block"


def test_settings_hot_flip_no_restart(make_environ):
    """A settings dict flip between calls takes effect immediately (hot read)."""
    ctx = make_environ({"audit_log": "/dev/null"})
    assert directive(ctx, "terminal", {"command": "git push -f origin main"})[0] == "block"
    ctx.settings["features"] = {"force_push_block": False, "outbound_approve": False}
    assert directive(ctx, "terminal", {"command": "git push -f origin main"}) == (None, None)

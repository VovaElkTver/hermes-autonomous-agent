"""Acceptance tests for plugins/skill-gate (outer-contour adapter)."""
import importlib.util
import json
import os
import types

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PLUGIN = os.path.join(REPO, "plugins", "skill-gate", "__init__.py")


def load_plugin():
    spec = importlib.util.spec_from_file_location("skill_gate_plugin", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


FAKE_GATE = """
import json, os, sys
cmd = sys.argv[1]
if cmd == "status":
    print(json.dumps({"r_best": 0.97, "next_iter": 12}))
    sys.exit(0)
# check: engine signals regression via exit 1
mode = os.environ.get("SG_MODE", "clean")
print(json.dumps({"r_val": 0.9, "r_best": 0.97, "drift": mode == "drift",
                  "regressed": {"v-x": [1.0, 0.0]} if mode == "drift" else {}}))
sys.exit(1 if mode == "drift" else 0)
"""


@pytest.fixture
def engine(tmp_path, monkeypatch):
    p = tmp_path / "gate.py"
    p.write_text(FAKE_GATE)
    mod = load_plugin()
    monkeypatch.setattr(mod, "_engine_dir", lambda: str(tmp_path))
    return mod, tmp_path


def test_check_clean(engine, monkeypatch):
    mod, tmp = engine
    monkeypatch.setenv("SG_MODE", "clean")
    verdict = json.loads(mod._handle_skill_gate("check"))
    assert verdict["ok"] is True and verdict["drift"] is False


def test_check_drift_is_verdict_not_failure(engine, monkeypatch):
    mod, tmp = engine
    monkeypatch.setenv("SG_MODE", "drift")
    verdict = json.loads(mod._handle_skill_gate("check"))
    assert verdict["ok"] is True, "exit 1 = a legitimate regression verdict"
    assert verdict["drift"] is True and "v-x" in verdict["regressed"]


def test_engine_absent_is_reported(tmp_path, monkeypatch):
    mod = load_plugin()
    monkeypatch.setattr(mod, "_engine_dir", lambda: str(tmp_path / "nowhere"))
    verdict = json.loads(mod._handle_skill_gate("check"))
    assert verdict["ok"] is False and "engine absent" in verdict["error"]


def test_unknown_action(engine):
    mod, _ = engine
    verdict = json.loads(mod._handle_skill_gate("launch-missiles"))
    assert verdict["ok"] is False and "check|status" in verdict["error"]


def test_registers_tool():
    mod = load_plugin()
    calls = []
    ctx = types.SimpleNamespace(register_tool=lambda **kw: calls.append(kw))
    mod.register(ctx)
    assert len(calls) == 1 and calls[0]["name"] == "skill_gate"
    assert "action" in calls[0]["schema"]["parameters"]["properties"]

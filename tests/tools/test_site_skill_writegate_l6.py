"""L6 write-gate on L9 site-skill replay (28.11): write-class steps are
skipped in replay by default, executed only with replay_writes: true, and
recorded as evidence in mint.json. No direct PyYAML (repo ban): skill YAML is
literal text parsed by the loader under test."""
import json

import pytest

from tools import site_skill as ss

SKILL_TEXT = """\
name: {name}
start_url: file:///x
steps:
  - action: wait_for_text
    text: Up
  - action: click
    selector: button.go
  - action: get_text
    selector: body
{extra}"""


def _skill(tmp_path, name="demo-gate", *, replay_writes=None):
    extra = "replay_writes: true" if replay_writes else ""
    d = tmp_path / name
    d.mkdir(parents=True)
    (d / "skill.yaml").write_text(SKILL_TEXT.format(name=name, extra=extra),
                                  encoding="utf-8")
    return d


@pytest.fixture()
def iso(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "SKILLS_DIR", tmp_path)
    executed = []

    def fake_run(start_url, step, *, fresh):
        executed.append(step["action"])
        return True, "", {}
    monkeypatch.setattr(ss, "_run_step", fake_run)
    monkeypatch.setattr(ss, "_ab", lambda *a, **k: None)
    return executed


def test_classification():
    assert ss._step_class("click") == "write"
    assert ss._step_class("press") == "write"
    assert ss._step_class("get_text") == "read"
    assert ss._step_class("wait_for_text") == "read"
    assert ss._step_class("scroll") == "read"


def test_replay_skips_writes_by_default(iso, tmp_path, capsys):
    _skill(tmp_path)
    assert ss.replay("demo-gate", screenshot_on_fail=False) == 0
    assert iso == ["wait_for_text", "get_text"]  # click never executed
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["skipped_write_steps"] == [1]


def test_replay_executes_writes_when_reviewed(iso, tmp_path, capsys):
    _skill(tmp_path, replay_writes=True)
    assert ss.replay("demo-gate", screenshot_on_fail=False) == 0
    assert iso == ["wait_for_text", "click", "get_text"]
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["skipped_write_steps"] == []


def test_mint_records_write_step_indices(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ss, "SKILLS_DIR", tmp_path)
    monkeypatch.setattr(ss, "_run_step",
                        lambda u, s, *, fresh: (True, "", {}))
    monkeypatch.setattr(ss, "_ab", lambda *a, **k: None)
    src = tmp_path / "src.yaml"
    src.write_text(SKILL_TEXT.format(name="demo-gate", extra=""),
                   encoding="utf-8")
    assert ss.mint(src) == 0
    mint = json.loads((tmp_path / "demo-gate" / "mint.json").read_text())
    assert mint["write_steps"] == [1]
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["write_steps"] == 1

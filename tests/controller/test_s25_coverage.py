"""S2.5 (ТЗ thinking-enable) — the judge sees meaning, not the tail.

Population (acceptance criteria) is owned by the contract; the run only
claims covered indices (src/claims shape: candidate doesn't declare the
population, it leaves a trace). Counterexample from the ТЗ: a green machine
predicate + empty criteria_covered must NOT pass — the gate lives code-side,
so a hallucinating or lazy judge cannot mint vacuous passes.
"""
import json
import sqlite3

import pytest

from controller import model, workers
from hermes_state_common import SCHEMA_SQL


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "state.db")
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA_SQL)
    yield c
    c.close()


def _task_with_step(conn, tmp_path, acceptance=None, facts=None):
    tid = model.create_task(conn, owner="vovak", workdir=str(tmp_path),
                            goal="починить прогон")
    # `acceptance or default` would swallow an intentional EMPTY population —
    # [] means "no criteria to grade against", None means "use defaults".
    acc = acceptance if acceptance is not None else [
        "pytest tests/controller зелёные",
        "бриф содержит секцию последствий"]
    fac = facts if facts is not None else ["критик подтвердил формат"]
    model.revise_contract(conn, tid, {"acceptance": acc, "facts": fac}, "editorial")
    sid = model.add_step(conn, tid, kind="do", title="шаг")
    return tid, sid


def _artifacts(rc=0):
    return [{"stdout_tail": "2 passed", "returncode": rc}]


class FakeJudge:
    def __init__(self, verdict="pass", reason="всё по критериям", covered=None,
                 degraded=False):
        self.args = verdict, reason, covered, degraded
        self.seen_criteria = "unset"

    def __call__(self, cfg, goal, history, criteria=None):
        v, r, cov, deg = self.args
        self.seen_criteria = criteria
        return {"verdict": v, "reason": r, "instruction": "",
                "covered": cov, "degraded": deg}


class TestCoverageGate:
    def test_pass_without_criteria_reaches_judge(self, conn, tmp_path, monkeypatch):
        import plugins.supervisor.judge as jd
        fake = FakeJudge(covered=[1, 2])
        monkeypatch.setattr(jd, "review", fake)
        tid, sid = _task_with_step(conn, tmp_path)
        rf = workers.make_review(conn, judge_cfg={})
        out = rf({"run_id": "r"}, {"step_id": sid, "task_id": tid,
                                   "title": "шаг"}, _artifacts())
        assert out["verdict"] == "pass"
        assert fake.seen_criteria == ["pytest tests/controller зелёные",
                                      "бриф содержит секцию последствий"]
        assert out["criteria_covered"] == [1, 2]

    def test_green_predicate_empty_coverage_is_revise(self, conn, tmp_path,
                                                      monkeypatch):
        # the ТЗ counterexample: judge says pass, names nothing -> vacuous
        import plugins.supervisor.judge as jd
        monkeypatch.setattr(jd, "review", FakeJudge(covered=[]))
        tid, sid = _task_with_step(conn, tmp_path)
        rf = workers.make_review(conn, judge_cfg={})
        out = rf({"run_id": "r"}, {"step_id": sid, "task_id": tid,
                                   "title": "шаг"}, _artifacts())
        assert out["verdict"] == "revise"
        assert "zero acceptance-criteria" in out["detail"]

    def test_no_criteria_no_population_no_gate(self, conn, tmp_path, monkeypatch):
        # a contract without acceptance: nothing to grade against, pass stands
        import plugins.supervisor.judge as jd
        monkeypatch.setattr(jd, "review", FakeJudge(covered=None))
        tid, sid = _task_with_step(conn, tmp_path, acceptance=[])
        rf = workers.make_review(conn, judge_cfg={})
        out = rf({"run_id": "r"}, {"step_id": sid, "task_id": tid,
                                   "title": "шаг"}, _artifacts())
        assert out["verdict"] == "pass"

    def test_degraded_pass_still_needs_human(self, conn, tmp_path, monkeypatch):
        import plugins.supervisor.judge as jd
        monkeypatch.setattr(jd, "review", FakeJudge(covered=[1], degraded=True))
        tid, sid = _task_with_step(conn, tmp_path)
        rf = workers.make_review(conn, judge_cfg={})
        out = rf({"run_id": "r"}, {"step_id": sid, "task_id": tid,
                                   "title": "шаг"}, _artifacts())
        assert out["verdict"] == "needs_human"

    def test_coverage_lands_in_review_row(self, conn, tmp_path, monkeypatch):
        import plugins.supervisor.judge as jd
        monkeypatch.setattr(jd, "review", FakeJudge(covered=[2]))
        tid, sid = _task_with_step(conn, tmp_path)
        rf = workers.make_review(conn, judge_cfg={})
        out = rf({"run_id": "r"}, {"step_id": sid, "task_id": tid,
                                   "title": "шаг"}, _artifacts())
        model.add_review(conn, sid, "r1", "h", out["verdict"], "controller-loop",
                         out["detail"], criteria_covered=out["criteria_covered"])
        row = conn.execute("SELECT criteria_covered FROM ctl_review").fetchone()
        assert json.loads(row["criteria_covered"]) == [2]


class TestJudgePromptAndParsing:
    def test_criteria_rendered_numbered_in_prompt(self):
        from plugins.supervisor.judge import build_messages
        msgs = build_messages("цель", [{"tool": "t", "status": "0",
                                        "args": "a", "result": "b"}],
                              12000, criteria=["критерий один", "критерий два"])
        user = msgs[1]["content"]
        assert "1. критерий один" in user and "2. критерий два" in user
        assert "covered" in user  # the contract for the extra field is spelled out

    def test_covered_is_clipped_to_population(self, monkeypatch):
        # hallucinated/out-of-range indices land in the claims "extra" bucket:
        # dropped, never counted — population is owned by the contract
        import plugins.supervisor.judge as jd

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self):
                return json.dumps({"choices": [{"message": {"content": json.dumps(
                    {"verdict": "pass", "reason": "ok",
                     "covered": [1, 9, -3, "2", "х"]})},
                    "finish_reason": "stop"}]}).encode()
        monkeypatch.setattr(jd.urllib.request, "urlopen", lambda *a, **k: Resp())
        out = jd.review({}, "g", [], criteria=["a", "b"])
        assert out["covered"] == [1, 2]

    def test_fail_open_keeps_covered_none(self, monkeypatch):
        import plugins.supervisor.judge as jd

        def boom(*a, **k):
            raise OSError("down")
        monkeypatch.setattr(jd.urllib.request, "urlopen", boom)
        out = jd.review({}, "g", [], criteria=["a"])
        assert out["degraded"] and out["covered"] is None


class TestDiffstatEvidence:
    def test_diffstat_shows_changed_files(self, tmp_path):
        import subprocess
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
        (tmp_path / "a.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=tmp_path, check=True)
        (tmp_path / "a.py").write_text("x = 2\n")
        (tmp_path / "new.py").write_text("y = 3\n")
        out = workers._diffstat(str(tmp_path))
        assert "a.py" in out and "new.py" in out

    def test_not_a_repo_is_empty_not_crash(self, tmp_path):
        assert workers._diffstat(str(tmp_path / "nope")) == ""

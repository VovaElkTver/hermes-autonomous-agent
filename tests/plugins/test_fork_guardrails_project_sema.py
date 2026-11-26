"""fork-guardrails 1.4 приёмка (ToDo «Про́риоцепция» §4, Вл 21.11):
семафор на ПРОЕКТ, не на каталог. Регрессия на 21.11: две сессии правили один
проект из разных подкаталогов/worktree и смешивали истории.

Проверяет, что checkout_lease реально берёт ресурсом корень репозитория
(не каталог файла) и чужая запись в любой его поддиректории блокируется, а
«findings-only» путь чужой сессии (писать вне проекта) остаётся свободным.
"""
import importlib.util
import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _outside_scratch(monkeypatch):
    # pytest exports TMPDIR=basetemp; prod never has projects there
    monkeypatch.delenv("TMPDIR", raising=False)

REPO = Path(__file__).resolve().parents[2]


def _mk_repo(root: Path) -> Path:
    repo = root / "proj"
    repo.mkdir(parents=True)
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=env)
    (repo / "README.md").write_text("seed\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "seed"], check=True, env=env)
    return repo


def _mk_state_db(path, rows):
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, title_source TEXT)")
    con.executemany("INSERT INTO sessions VALUES (?, ?, ?)", rows)
    con.commit()
    con.close()


def _load_plugin(tmp_path, state_db):
    spec = importlib.util.spec_from_file_location(
        "fg_sema_under_test", REPO / "plugins" / "fork-guardrails" / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    settings = {"enabled": True, "log_only": False, "features": {},
                "audit_log": str(tmp_path / "fg" / "audit.jsonl"),
                "state_db": str(state_db)}

    class _Ctx:
        @staticmethod
        def get_config(key, fallback=None):
            return settings.get(key, fallback)

    mod._ctx = _Ctx()
    mod._REPO_CACHE.clear()
    return mod


def _write(mod, sid, path, turn="t1"):
    return mod.pre_tool_call(tool_name="write_file",
                             args={"path": str(path), "content": "x"},
                             task_id="k", session_id=sid, tool_call_id="c",
                             turn_id=turn, api_request_id="a", middleware_trace=[])


def test_project_semaphore_across_subdirs(tmp_path):
    db = tmp_path / "state.db"
    _mk_state_db(db, [("s1", "levels-build", "user"), ("s2", "grpo-smoke", "user")])
    mod = _load_plugin(tmp_path, db)
    repo = _mk_repo(tmp_path)
    (repo / "sub" / "deep").mkdir(parents=True)
    r1 = _write(mod, "s1", repo / "a.md")
    assert not (isinstance(r1, dict) and r1.get("action") == "block"), r1
    r2 = _write(mod, "s2", repo / "sub" / "deep" / "b.md")  # другой подкаталог того же проекта
    blob = json.dumps(r2, default=str)
    assert r2 and '"block"' in blob, r2
    assert "levels-build" in blob


def test_worktree_of_same_project_is_foreign_too(tmp_path):
    db = tmp_path / "state.db"
    _mk_state_db(db, [("s1", "levels-build", "user"), ("s2", "grpo-smoke", "user")])
    mod = _load_plugin(tmp_path, db)
    repo = _mk_repo(tmp_path)
    wt = tmp_path / "wt"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", "side", str(wt)], check=True)
    _write(mod, "s1", repo / "a.md")
    r2 = _write(mod, "s2", wt / "b.md")
    blob = json.dumps(r2, default=str)
    # либо worktree резолвится в корень основного проекта (блок), либо это отдельный
    # ресурс — но тогда в блоке/реестре видно, ЧТО это тот же проект; двойная запись
    # в один проект из двух сессий недопустима ни в каком из вариантов.
    assert r2 and '"block"' in blob, r2


def test_foreign_session_findings_path_stays_free(tmp_path):
    db = tmp_path / "state.db"
    _mk_state_db(db, [("s1", "levels-build", "user"), ("s2", "grpo-smoke", "user")])
    mod = _load_plugin(tmp_path, db)
    repo = _mk_repo(tmp_path)
    _write(mod, "s1", repo / "a.md")
    outdir = tmp_path / "findings"  # не в проекте s1
    r = _write(mod, "s2", outdir / "notes.md")
    assert not (isinstance(r, dict) and r.get("action") == "block"), r

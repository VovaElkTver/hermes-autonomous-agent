"""Stage003 step 7: implicit-topic detector (controller.topic_detector).

Contract (ToDo stage003 п.7 / отчёт topic-memory-integration, шаг 7): the
detector is an OBSERVER for interactive conversations — one ctl_topic_detect
row per observed turn, gate ``memory.topic_detect_enabled`` default OFF
(«ноль байт»), corpus = active ctl_task goals + prior proposals, embedder =
the sqlite memory plugin's endpoint with lexical fallback, fail-open
everywhere. Nothing reads the rows back into behaviour before Вл's
acceptance; these tests pin the shadow-only surface.
"""
import time

import pytest


@pytest.fixture()
def conn(tmp_path):
    from controller import model
    c = model.connect(tmp_path / "state.db")
    yield c
    c.close()


def _enable(monkeypatch):
    monkeypatch.setattr("controller.topic_detector.enabled", lambda config=None: True)


def _add_task(conn, task_id, goal, state="in_progress"):
    now = time.time()
    conn.execute(
        "INSERT INTO ctl_task (task_id, owner, workdir, goal, state,"
        " contract_version, created_at, updated_at)"
        " VALUES (:task_id, 'vovak', '', :goal, :state, 1, :now, :now)",
        {"task_id": task_id, "goal": goal, "state": state, "now": now})
    conn.commit()


def test_table_exists_in_canonical_schema(conn):
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE name = 'ctl_topic_detect'").fetchall()}
    assert "ctl_topic_detect" in names


def test_gate_off_is_inert_and_writes_nothing(conn, monkeypatch):
    from controller import topic_detector
    monkeypatch.delenv("HERMES_SCOPE_ID", raising=False)
    _add_task(conn, "task-a", "выбор хранилища для кэша")
    result = topic_detector.detect("sess-1", "про выбор хранилища для кэша",
                                   conn=conn,
                                   config={"memory": {"topic_detect_enabled": False}})
    assert result is None
    assert conn.execute("SELECT COUNT(*) AS n FROM ctl_topic_detect").fetchone()["n"] == 0


def test_lexical_fallback_matches_active_task_goal(conn, monkeypatch):
    """Embedder down (httpx.post raises): classification still accrues."""
    import httpx
    def _boom(*a, **k):
        raise OSError("embedder down")
    monkeypatch.setattr(httpx, "post", _boom)
    _enable(monkeypatch)
    from controller import topic_detector
    _add_task(conn, "task-store", "выбор хранилища для кэша приложения")
    row = topic_detector.detect("sess-1", "нужен выбор хранилища для кэша", conn=conn)
    assert row is not None
    assert row["method"] == "lexical"
    assert row["best_scope_id"] == "task-store"
    assert row["proposed_new"] is None
    stored = conn.execute(
        "SELECT * FROM ctl_topic_detect WHERE detect_id LIKE 'ctdet-%'"
        " ORDER BY created_at DESC LIMIT 1").fetchone()
    assert stored["best_scope_id"] == "task-store"


def test_no_match_proposes_new_topic(conn, monkeypatch):
    import httpx
    monkeypatch.setattr(httpx, "post", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    _enable(monkeypatch)
    from controller import topic_detector
    _add_task(conn, "task-store", "выбор хранилища для кэша")
    row = topic_detector.detect("sess-1", "позвони электрику завтра утром", conn=conn)
    assert row is not None
    assert row["method"] == "lexical"
    assert row["proposed_new"]  # unmatched excerpt recorded as candidate
    assert row["best_scope_id"] in (None, "task-store")  # sub-threshold kept as evidence


def test_embed_path_matches_on_cosine(conn, monkeypatch):
    """Fake endpoint: message and goal embed to the same vector -> match."""
    import httpx

    class _Resp:
        def __init__(self, vec):
            self._vec = vec
        def raise_for_status(self):
            pass
        def json(self):
            return {"data": [{"embedding": self._vec}]}

    vec = [1.0, 0.0, 0.0]
    far = [0.0, 1.0, 0.0]
    texts = {"мне надо выбрать хранилище": vec, "выбор хранилища": vec, "прочее": far}

    def _post(url, json=None, timeout=None):
        return _Resp(texts[json["input"]])
    monkeypatch.setattr(httpx, "post", _post)
    _enable(monkeypatch)
    from controller import topic_detector
    topic_detector._EMBED_CACHE.clear()
    _add_task(conn, "task-store", "выбор хранилища")
    row = topic_detector.detect("sess-1", "мне надо выбрать хранилище", conn=conn)
    assert row is not None
    assert row["method"] == "embed"
    assert row["best_scope_id"] == "task-store"
    assert row["score"] > 0.99
    assert row["proposed_new"] is None


def test_detect_never_raises_on_broken_db(monkeypatch):
    _enable(monkeypatch)
    from controller import topic_detector

    class _Broken:
        def execute(self, *a, **k):
            raise RuntimeError("db gone")
        def commit(self):
            pass
    assert topic_detector.detect("sess-1", "что-то", conn=_Broken()) is None


def test_counts_always_has_three_method_keys(conn, monkeypatch):
    _enable(monkeypatch)
    import httpx
    monkeypatch.setattr(httpx, "post", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    from controller import topic_detector
    _add_task(conn, "task-store", "выбор хранилища")
    topic_detector.detect("sess-1", "про выбор хранилища", conn=conn)
    out = topic_detector.counts(conn)
    assert set(out) == {"embed", "lexical", "skip"}
    assert out["lexical"] >= 1


def test_closed_tasks_leave_the_corpus(conn, monkeypatch):
    _enable(monkeypatch)
    import httpx
    monkeypatch.setattr(httpx, "post", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    from controller import topic_detector
    _add_task(conn, "task-done", "выбор хранилища для кэша", state="done")
    row = topic_detector.detect("sess-1", "про выбор хранилища", conn=conn)
    assert row["corpus_size"] == 0
    assert row["method"] == "skip"
    assert row["proposed_new"]

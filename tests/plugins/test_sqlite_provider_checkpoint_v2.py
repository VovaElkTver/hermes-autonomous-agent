"""Bundled sqlite memory provider: pre-compress checkpoint API v2 behavior.

The host refuses compression fail-closed when a v2 provider cannot guarantee a
snapshot (compression.checkpoint_required). These tests pin the provider side of
that contract: durability, verification, propagation, retention.
"""

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from agent.memory_manager import MemoryManager
from agent.memory_provider import PRE_COMPRESS_CHECKPOINT_API_VERSION

_PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "memory" / "sqlite" / "__init__.py"


def _load_provider_class():
    spec = importlib.util.spec_from_file_location("bundled_sqlite_memory", _PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.SqliteMemoryProvider


SqliteMemoryProvider = _load_provider_class()

EVIDENCE = [
    {"role": "user", "content": "решение: мерж 53"},
    {"role": "assistant", "content": "PR merged, task closed"},
]


@pytest.fixture()
def provider(tmp_path):
    prov = SqliteMemoryProvider(config={"migrate_from": ""})
    prov.initialize("sess-test", hermes_home=str(tmp_path), agent_identity="test")
    yield prov
    prov.shutdown()


def test_provider_advertises_checkpoint_api_v2():
    assert SqliteMemoryProvider.pre_compress_checkpoint_api_version == PRE_COMPRESS_CHECKPOINT_API_VERSION
    manager = MemoryManager()
    manager.add_provider(SqliteMemoryProvider(config={"migrate_from": ""}))
    assert manager.supports_pre_compress_checkpoint(PRE_COMPRESS_CHECKPOINT_API_VERSION)


def test_required_checkpoint_is_durable_and_readable_back(provider, tmp_path):
    provider.on_pre_compress(EVIDENCE, require_checkpoint=True)
    rows = provider.checkpoint_list()
    assert len(rows) == 1
    payload = provider.checkpoint_payload(rows[0]["id"])
    assert payload == json.loads(json.dumps(EVIDENCE))
    conn = sqlite3.connect(f"file:{tmp_path / 'memory.sqlite'}?mode=ro", uri=True)
    try:
        stored = conn.execute("SELECT payload FROM checkpoints").fetchone()[0]
    finally:
        conn.close()
    assert json.loads(stored) == EVIDENCE


def test_required_checkpoint_propagates_store_failure(provider, monkeypatch):
    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("disk gone")

    monkeypatch.setattr(sqlite3, "connect", boom)
    with pytest.raises(sqlite3.OperationalError):
        provider.on_pre_compress(EVIDENCE, require_checkpoint=True)


def test_best_effort_mode_swallows_store_failure(provider, monkeypatch):
    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("disk gone")

    monkeypatch.setattr(sqlite3, "connect", boom)
    assert provider.on_pre_compress(EVIDENCE, require_checkpoint=False) == ""


def test_retention_bounds_checkpoint_history(provider, monkeypatch):
    monkeypatch.setitem(provider._config, "checkpoint_keep", 3)
    for i in range(6):
        provider.on_pre_compress([{"role": "user", "content": f"turn {i}"}], require_checkpoint=True)
    rows = provider.checkpoint_list(limit=50)
    assert len(rows) == 3
    newest = provider.checkpoint_payload(rows[0]["id"])
    assert newest == [{"role": "user", "content": "turn 5"}]


def test_corrupted_checkpoint_content_fails_integrity_recheck(provider, monkeypatch):
    real_connect = sqlite3.connect

    class CorruptingConn:
        """Returns a mismatched payload only for the read-only verify connection."""

        def __init__(self, *args, **kwargs):
            target = args[0] if args else kwargs.get("database", "")
            self._ro = "mode=ro" in str(target)
            self._inner = real_connect(*args, **kwargs)

        def execute(self, sql, *a, **kw):
            cur = self._inner.execute(sql, *a, **kw)
            if self._ro and "SELECT payload FROM checkpoints WHERE sha" in sql:
                row = cur.fetchone()
                if row is not None:
                    original = row[0]
                    tampered = original[:-2] + ("00" if not original.endswith("00") else "11")

                    class _Cur:
                        def fetchone(self_inner):
                            return (tampered,)

                    return _Cur()
            return cur

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def close(self):
            self._inner.close()

    monkeypatch.setattr(sqlite3, "connect", lambda *a, **kw: CorruptingConn(*a, **kw))
    with pytest.raises(sqlite3.DatabaseError, match="integrity"):
        provider.on_pre_compress(EVIDENCE, require_checkpoint=True)

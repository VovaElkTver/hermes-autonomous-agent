"""Tier L5 (TZ-tinyhumansai) regression: admission gate, exact-duplicate idempotency,
norm migration/backfill, namespace scope. Providers run on tmp DBs; embeddings stubbed."""

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "memory" / "sqlite" / "__init__.py"


def _load_plugin_module():
    spec = importlib.util.spec_from_file_location("sqlite_memory_plugin_under_test", PLUGIN)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _provider(mod, home, profile, **cfg):
    p = mod.SqliteMemoryProvider(config={"admission_min_chars": 0, **cfg})
    p._embed = lambda text: [float(len(text) % 7), 1.0, 0.5]
    p.initialize("test-session", hermes_home=str(home), agent_identity=profile)
    return p


def _call(p, **args):
    return json.loads(p.handle_tool_call("sqlite_memory", args))


def test_duplicate_admission_idempotent(tmp_path):
    mod = _load_plugin_module()
    p = _provider(mod, tmp_path, "alpha")
    text = "the gateway unit name is resolved dynamically via systemctl"
    r1 = _call(p, action="add", text=text)
    r2 = _call(p, action="add", text="THE  Gateway   Unit Name IS RESOLVED DYNAMICALLY VIA SYSTEMCTL")
    assert r1["ok"] and r2["id"] == r1["id"]
    p.shutdown()


def test_min_chars_gate_rejects_fragment(tmp_path):
    mod = _load_plugin_module()
    p = _provider(mod, tmp_path, "alpha", admission_min_chars=24)
    r = _call(p, action="add", text="short fact")
    assert "error" in r and "admission" in r["error"]
    r_ok = _call(p, action="add", text="a durable statement long enough to pass the admission gate")
    assert r_ok.get("ok")
    p.shutdown()


def test_scope_filters_namespace(tmp_path):
    mod = _load_plugin_module()
    a = _provider(mod, tmp_path, "alpha")
    r1 = _call(a, action="add", text="namespace alpha durable statement about routing rules")
    assert r1["ok"]
    b = _provider(mod, tmp_path, "beta")
    q = "namespace alpha routing"
    res_all = _call(b, action="search", text=q)["results"]
    assert any(x["id"] == r1["id"] for x in res_all), "global search must see other namespaces"
    res_b = _call(b, action="search", text=q, scope="beta")["results"]
    assert all(x["id"] != r1["id"] for x in res_b), "scope=beta must hide alpha's fact"
    res_a = _call(b, action="search", text=q, scope="alpha")["results"]
    assert any(x["id"] == r1["id"] for x in res_a)
    a.shutdown(); b.shutdown()


def test_legacy_db_migration_backfills_norm(tmp_path):
    mod = _load_plugin_module()
    home = tmp_path / "legacy"; home.mkdir()
    db = home / "memory.sqlite"
    con = sqlite3.connect(db)
    con.executescript("""
      CREATE TABLE facts(id TEXT PRIMARY KEY, text TEXT NOT NULL, kind TEXT DEFAULT 'fact',
        source_profile TEXT, author TEXT, created_at TEXT, valid_at TEXT, invalid_at TEXT, embedding BLOB);
      INSERT INTO facts VALUES('old1','Legacy  Row   Text','fact','p',NULL,NULL,NULL,NULL,NULL);
    """)
    con.commit(); con.close()
    p = mod.SqliteMemoryProvider(config={"admission_min_chars": 0})
    p._embed = lambda text: [1.0, 0.0]
    p.initialize("test-session", hermes_home=str(home), agent_identity="p")
    con = sqlite3.connect(db)
    norm = con.execute("SELECT norm FROM facts WHERE id='old1'").fetchone()[0]
    con.close()
    assert norm == "legacy row text"
    # duplicate admission must catch legacy rows too
    r = _call(p, action="add", text="legacy row text")
    assert r["id"] == "old1"
    p.shutdown()

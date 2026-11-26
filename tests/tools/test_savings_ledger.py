"""S2 (TZ-memdoor-5090-v2): savings-ledger acceptance.

Acceptance (TZ): seeded run (large stdout) → one line in the ledger; ``hermes
savings`` total = python recompute of the file; OFF → no file. The ledger is
flag-gated (default OFF): when off, record() is a no-op and the file is never
created.
"""

import argparse
import json

import pytest

from tools import savings_ledger


@pytest.fixture(autouse=True)
def _clean_cache():
    savings_ledger._clear_cache()
    yield
    savings_ledger._clear_cache()


def _seed(path, events):
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(json.dumps(e) + "\n" for e in events)


def _lines(path):
    return [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]


def test_off_no_file(tmp_path):
    ledger = tmp_path / "savings.jsonl"
    assert savings_ledger.record("execute_code", raw_b=1000, kept_b=200,
                                 path=str(ledger)) is False
    assert not ledger.exists()


def test_on_appends_one_line(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_SAVINGS_LEDGER", "1")
    ledger = tmp_path / "savings.jsonl"
    assert savings_ledger.record("execute_code", raw_b=1000, kept_b=200,
                                 event="stdout", path=str(ledger)) is True
    lines = _lines(ledger)
    assert len(lines) == 1
    assert lines[0]["tool"] == "execute_code"
    assert lines[0]["raw_b"] == 1000 and lines[0]["kept_b"] == 200
    assert lines[0]["event"] == "stdout" and "ts" in lines[0]


def test_summarize_matches_python_recompute(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_SAVINGS_LEDGER", "1")
    ledger = tmp_path / "savings.jsonl"
    events = [
        {"ts": 1.0, "tool": "execute_code", "raw_b": 10000, "kept_b": 2000},
        {"ts": 2.0, "tool": "web_extract", "raw_b": 5000, "kept_b": 1000},
        {"ts": 3.0, "tool": "execute_code", "raw_b": 3000, "kept_b": 3000},  # nothing saved
    ]
    _seed(ledger, events)
    s = savings_ledger.summarize(path=str(ledger))
    raw = sum(e["raw_b"] for e in events)
    kept = sum(e["kept_b"] for e in events)
    saved = max(0, raw - kept)
    assert s["raw_b"] == raw and s["kept_b"] == kept and s["saved_b"] == saved
    assert s["approx_tokens"] == saved // 4
    assert s["events"] == 3
    assert s["approx_usd_floor"] is None                 # price not supplied → n/a
    s2 = savings_ledger.summarize(path=str(ledger), price_usd_per_mtok=0.40)
    assert s2["approx_usd_floor"] == pytest.approx((saved // 4) / 1e6 * 0.40)


def test_code_execution_hook_writes_line(tmp_path, monkeypatch):
    import tools.code_execution_tool as cet
    monkeypatch.setenv("HERMES_SAVINGS_LEDGER", "1")
    ledger = tmp_path / "savings.jsonl"
    monkeypatch.setattr(savings_ledger, "ledger_path", lambda: str(ledger))
    monkeypatch.setattr(cet, "_spill_full_stdout", lambda text: None)      # no spill side-effect
    big = "x" * 100_000                                                  # > MAX_STDOUT_BYTES
    _text, meta = cet._truncate_stdout_text(big)
    assert meta["stdout_truncated"] is True
    lines = _lines(ledger)
    assert len(lines) == 1
    assert lines[0]["tool"] == "execute_code"
    assert lines[0]["raw_b"] == len(big.encode("utf-8", "replace"))
    assert lines[0]["kept_b"] == cet.MAX_STDOUT_BYTES


def test_web_extract_hook_writes_line(tmp_path, monkeypatch):
    import tools.web_tools_truncate as wt
    monkeypatch.setenv("HERMES_SAVINGS_LEDGER", "1")
    ledger = tmp_path / "savings.jsonl"
    monkeypatch.setattr(savings_ledger, "ledger_path", lambda: str(ledger))
    monkeypatch.setattr(wt, "_store_full_text", lambda url, content: None)
    content = "line\n" * 20_000
    _out, was_truncated = wt._truncate_with_footer(content, "http://x", 5000)
    assert was_truncated is True
    lines = _lines(ledger)
    assert len(lines) == 1
    assert lines[0]["tool"] == "web_extract"


def test_cli_savings_matches_recompute(tmp_path, monkeypatch, capsys):
    from hermes_cli import savings_cmd
    ledger = tmp_path / "savings.jsonl"
    _seed(ledger, [{"ts": 1.0, "tool": "execute_code", "raw_b": 10000, "kept_b": 2000}])
    monkeypatch.setattr(savings_ledger, "ledger_path", lambda: str(ledger))
    args = argparse.Namespace(month=None, price_per_mtok=None, json=False)
    rc = savings_cmd.cmd_savings(args)
    out = capsys.readouterr().out
    saved = 10000 - 2000
    assert rc == 0
    assert f"not-in-context:    {saved:,} B" in out
    assert f"≈ tokens (b/4):    {saved // 4:,}" in out
    assert "n/a" in out                                        # no price → $ floor n/a


def test_cli_savings_json(tmp_path, monkeypatch, capsys):
    from hermes_cli import savings_cmd
    ledger = tmp_path / "savings.jsonl"
    _seed(ledger, [{"ts": 1.0, "tool": "execute_code", "raw_b": 10000, "kept_b": 2000}])
    monkeypatch.setattr(savings_ledger, "ledger_path", lambda: str(ledger))
    args = argparse.Namespace(month=None, price_per_mtok=None, json=True)
    assert savings_cmd.cmd_savings(args) == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["saved_b"] == 8000 and data["approx_tokens"] == 2000

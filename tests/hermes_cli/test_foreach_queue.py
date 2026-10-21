"""Tests for the /for queue-forcing helper (hermes_cli.foreach_queue)."""

import os

import pytest

from hermes_cli.foreach_queue import (
    DEFAULT_LIMIT,
    ENFORCEMENT_NOTE,
    build_plan,
    expand_items,
    parse_for_args,
)


def _mk(tmp_path, names):
    d = tmp_path / "catalog"
    d.mkdir()
    for n in names:
        (d / n).write_text("x", encoding="utf-8")
    return d


def test_parse_args_basic_limit_and_instruction():
    parsed = parse_for_args("/data/corpus summarize each --limit 5")
    assert parsed["path"] == "/data/corpus"
    assert parsed["instruction"] == "summarize each"
    assert parsed["limit"] == 5


def test_parse_args_go_flag():
    parsed = parse_for_args("/data/corpus индексировать --go")
    assert parsed["go"] is True
    assert parsed["path"] == "/data/corpus"
    assert parsed["instruction"] == "индексировать"
    assert parse_for_args("/data/corpus")["go"] is False
    assert parse_for_args("/x a --limit 3 --go")["limit"] == 3
    with pytest.raises(ValueError, match="before any flags"):
        parse_for_args("--go")


def test_parse_args_requires_path():
    with pytest.raises(ValueError):
        parse_for_args("   ")
    with pytest.raises(ValueError):
        parse_for_args("x --limit nope")


def test_expand_items_dir_skips_dotfiles_sorted(tmp_path):
    d = _mk(tmp_path, ["b.md", ".hidden", "a.md"])
    workdir, items, trunc = expand_items(str(d))
    assert [os.path.basename(i) for i in items] == ["a.md", "b.md"]
    assert trunc == 0
    assert workdir == str(d)


def test_expand_items_glob_and_missing(tmp_path):
    _mk(tmp_path, ["1.pdf", "2.pdf", "note.txt"])
    _workdir, items, trunc = expand_items(str(tmp_path / "catalog" / "*.pdf"))
    assert len(items) == 2 and trunc == 0
    with pytest.raises(ValueError, match="no files matched"):
        expand_items(str(tmp_path / "catalog" / "*.zzz"))
    with pytest.raises(ValueError, match="does not exist"):
        expand_items(str(tmp_path / "nope"))


def test_build_plan_enforces_queue_loop(tmp_path):
    d = _mk(tmp_path, ["a.md", "b.md", "c.md"])
    plan = build_plan(str(d), "Make an annotated index entry", limit=DEFAULT_LIMIT)
    assert [s["title"] for s in plan.steps] == [
        "1. a.md", "2. b.md", "3. c.md"]
    assert plan.workdir == str(d)
    assert ENFORCEMENT_NOTE in plan.goal
    assert "annotated index" in plan.goal
    assert any("blocked" in a or "silently" in a for a in plan.acceptance)


def test_build_plan_reports_truncation(tmp_path):
    d = _mk(tmp_path, [f"f{i}.md" for i in range(5)])
    plan = build_plan(str(d), "", limit=3)
    assert len(plan.steps) == 3
    assert plan.truncated == 2

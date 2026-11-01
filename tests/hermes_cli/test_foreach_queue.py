"""Tests for the /for queue-forcing helper (hermes_cli.foreach_queue)."""

import os

import pytest

from hermes_cli.foreach_queue import (
    DEFAULT_LIMIT,
    ENFORCEMENT_NOTE,
    build_plan,
    expand_items,
    parse_for_args,
    resolve_set,
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


# --- set-builder form: /for <A> in <set> ... --------------------------------

def test_parse_args_binding_form():
    parsed = parse_for_args("card in ~/corpus/*.md переписать <card> по шаблону")
    assert parsed["var"] == "card"
    assert parsed["path"] == "~/corpus/*.md"
    assert parsed["instruction"] == "переписать <card> по шаблону"
    assert parsed["go"] is False


def test_parse_args_binding_unicode_var_and_flags():
    parsed = parse_for_args("файл in /data/incoming привести к схеме --limit 5 --go")
    assert parsed["var"] == "файл"
    assert parsed["path"] == "/data/incoming"
    assert parsed["instruction"] == "привести к схеме"
    assert parsed["limit"] == 5
    assert parsed["go"] is True


def test_parse_args_binding_literal_set_with_spaces():
    parsed = parse_for_args("issue in {ABC-1, DEF-2, GHI 3} закрыть --go")
    assert parsed["var"] == "issue"
    assert parsed["path"] == "{ABC-1, DEF-2, GHI 3}"
    assert parsed["instruction"] == "закрыть"
    assert parsed["go"] is True


def test_legacy_positional_never_binds_path_tokens():
    # A slash-bearing first token is never a binding variable, even if an
    # instruction later starts with the bare word "in".
    parsed = parse_for_args("/data/corpus in english summarize")
    assert parsed["var"] is None
    assert parsed["path"] == "/data/corpus"
    assert parsed["instruction"] == "in english summarize"


def test_binding_requires_set_after_in():
    with pytest.raises(ValueError, match="after 'in'"):
        parse_for_args("x in --go")


# --- set designations --------------------------------------------------------

def test_resolve_set_literal():
    workdir, items, trunc = resolve_set("{a, b-2, c}")
    assert items == ["a", "b-2", "c"]
    assert trunc == 0
    assert os.path.isdir(workdir)
    with pytest.raises(ValueError, match="empty literal set"):
        resolve_set("{}")


def test_resolve_set_at_file(tmp_path):
    lst = tmp_path / "ids.txt"
    lst.write_text("# comment\n\nurl-one\nurl-two\n", encoding="utf-8")
    workdir, items, trunc = resolve_set(f"@{lst}")
    assert items == ["url-one", "url-two"]
    assert workdir == str(tmp_path)
    assert trunc == 0
    with pytest.raises(ValueError, match="set file does not exist"):
        resolve_set("@/nope/missing.txt")


def test_resolve_set_catalog_labels_basenames(tmp_path):
    d = _mk(tmp_path, ["a.md", "b.md"])
    workdir, items, trunc = resolve_set(str(d))
    assert items == ["a.md", "b.md"]
    assert workdir == str(d)
    assert trunc == 0


# --- expansion / plan ---------------------------------------------------------

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
    assert plan.var is None
    assert ENFORCEMENT_NOTE in plan.goal
    assert "annotated index" in plan.goal
    assert any("blocked" in a or "silently" in a for a in plan.acceptance)


def test_build_plan_binding_titles_and_contract(tmp_path):
    d = _mk(tmp_path, ["a.md", "b.md"])
    plan = build_plan(str(d), "перевести <файл> в pdf", var="файл")
    assert [s["title"] for s in plan.steps] == ["1. файл = a.md", "2. файл = b.md"]
    assert plan.var == "файл"
    assert f"<файл> in {d}" in plan.goal
    assert "Binding:" in plan.goal and "exactly that item" in plan.goal


def test_build_plan_reports_truncation(tmp_path):
    d = _mk(tmp_path, [f"f{i}.md" for i in range(5)])
    plan = build_plan(str(d), "", limit=3)
    assert len(plan.steps) == 3
    assert plan.truncated == 2

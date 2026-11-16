"""B5 guard: eval signals never enter the model's input (prompt assembly).

Mechanical half of the doctrine (docs/design/eval-signals-contour.md):
the prompt-assembly surface group must not carry eval-signal names.
The guard is pure stdlib and runs on the bare CI checkout; these tests
drive it on synthetic trees plus the real surface group.
"""
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _load_guard():
    spec = importlib.util.spec_from_file_location(
        "check_eval_signals", REPO / "scripts/ci/check_eval_signals.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_eval_signals"] = mod
    spec.loader.exec_module(mod)
    return mod


guard = _load_guard()

SURFACE_TEMPLATE = {
    "agent/prompt_builder.py": "def build(parts):\n    return parts\n",
    "agent/skills_index_delta.py": "def delta(index):\n    return index\n",
    "agent/prompt_caching.py": "def cache(prompt):\n    return prompt\n",
    "agent/turn_context_compaction.py": "def compact(turns):\n    return turns\n",
}


def _tree(root: Path, contents=None) -> Path:
    for rel, text in (contents or SURFACE_TEMPLATE).items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root


def test_real_surface_group_is_clean():
    violations, inventory_errors = guard.check(REPO)
    assert not inventory_errors, inventory_errors
    assert not violations, violations


def test_planted_gepa_token_fails(tmp_path):
    contents = dict(SURFACE_TEMPLATE)
    contents["agent/prompt_builder.py"] = (
        "def build(parts):\n    # gepa corpus injected here\n    return parts\n")
    _tree(tmp_path, contents)
    violations, errors = guard.check(tmp_path)
    assert not errors
    assert any("gepa" in v for v in violations)


def test_planted_eval_label_fails(tmp_path):
    contents = dict(SURFACE_TEMPLATE)
    contents["agent/skills_index_delta.py"] = "LABEL = 'eval_label'\n"
    _tree(tmp_path, contents)
    violations, _ = guard.check(tmp_path)
    assert any("eval_label" in v for v in violations)


def test_feedback_identifier_in_code_fails(tmp_path):
    contents = dict(SURFACE_TEMPLATE)
    contents["agent/prompt_caching.py"] = "import feedback\n\ndef f():\n    feedback.inject()\n"
    _tree(tmp_path, contents)
    violations, errors = guard.check(tmp_path)
    assert not errors
    assert any("'feedback'" in v for v in violations)


def test_feedback_in_prose_is_out_of_scope(tmp_path):
    contents = dict(SURFACE_TEMPLATE)
    contents["agent/turn_context_compaction.py"] = (
        '# user feedback loops are documented elsewhere\n'
        "def compact(turns):\n    '''feedback from the user is kept'''\n    return turns\n")
    _tree(tmp_path, contents)
    violations, _ = guard.check(tmp_path)
    assert not violations, violations


def test_missing_surface_file_fails_closed(tmp_path):
    _tree(tmp_path)
    (tmp_path / "agent/prompt_builder.py").unlink()
    _, errors = guard.check(tmp_path)
    assert any("prompt_builder" in e for e in errors)


def test_unparseable_surface_file_fails_closed(tmp_path):
    contents = dict(SURFACE_TEMPLATE)
    contents["agent/prompt_caching.py"] = "def broken(:\n"
    _tree(tmp_path, contents)
    _, errors = guard.check(tmp_path)
    assert any("prompt_caching" in e for e in errors)

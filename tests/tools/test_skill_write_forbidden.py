"""Tests for ``skills.write_forbidden`` (tools/skill_write_policy.py).

The gate: when the config key is true, NOTHING is written into the skill library — every
would-be write lands as a plain file under ``~/.hermes/need2write/`` mirroring the target
path (delete/archive intents as empty ``__del``/``__archive`` markers, collisions get
``__2``/``__3``). When false/absent, behavior is byte-identical to before.
"""

from __future__ import annotations

import json
import shutil
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.skill_manager_tool import _create_skill, _find_skill, skill_manage
from tools import skill_write_policy


VALID = """\
---
name: test-skill
description: A test skill for the write ban.
---

# Test Skill

Original body.
"""

PATCHED = VALID.replace("Original body.", "Patched body.")


@pytest.fixture
def n2w_env(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with one on-disk skill; config.yaml drives the ban for real
    (write_forbidden() reads it live). Returns (home, skills_dir, need2write)."""
    home = tmp_path / ".hermes"
    skills = home / "skills"
    skills.mkdir(parents=True)
    (skills / "test-skill").mkdir()
    (skills / "test-skill" / "SKILL.md").write_text(VALID, encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))

    @contextmanager
    def _patched_dirs():
        with patch("tools.skill_manager_tool.SKILLS_DIR", skills), \
             patch("agent.skill_utils.get_all_skills_dirs", return_value=[skills]):
            yield

    def set_forbidden(value):
        (home / "config.yaml").write_text(
            f"skills:\n  write_forbidden: {json.dumps(value)}\n", encoding="utf-8")

    yield {"home": home, "skills": skills, "n2w": home / "need2write",
           "dirs": _patched_dirs, "set_forbidden": set_forbidden}


def tree_bytes(root: Path) -> dict[str, bytes]:
    """Byte snapshot of every regular file under *root* (relative path -> content).
    Transient ``.locks/`` lock files are infrastructure, not library content."""
    return {str(p.relative_to(root)): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()
            and not str(p.relative_to(root)).startswith(".locks" + "/")}


# --- the config flag itself -----------------------------------------------------

def test_flag_absent_means_allowed(n2w_env):
    with n2w_env["dirs"]():
        assert skill_write_policy.write_forbidden() is False
        n2w_env["set_forbidden"](False)
        assert skill_write_policy.write_forbidden() is False


def test_flag_is_read_live_at_action_time(n2w_env):
    """Flipping config.yaml between two calls changes the verdict without a restart."""
    with n2w_env["dirs"]():
        assert skill_write_policy.write_forbidden() is False
        n2w_env["set_forbidden"](True)
        assert skill_write_policy.write_forbidden() is True
        n2w_env["set_forbidden"](False)
        assert skill_write_policy.write_forbidden() is False


# --- forbidden=false: the normal branch is untouched ----------------------------

def test_allowed_branch_writes_normally(n2w_env):
    n2w_env["set_forbidden"](False)
    with n2w_env["dirs"]():
        res = _create_skill("fresh-skill", VALID.replace("test-skill", "fresh-skill"))
        assert res["success"]
        assert (n2w_env["skills"] / "fresh-skill" / "SKILL.md").is_file()
        assert not n2w_env["n2w"].exists()


# --- forbidden=true: skill_manage surface ----------------------------------------

def test_patch_dumps_full_text_and_answers_no_retry(n2w_env):
    n2w_env["set_forbidden"](True)
    before = tree_bytes(n2w_env["skills"])
    with n2w_env["dirs"]():
        raw = json.loads(skill_manage(
            action="patch", name="test-skill",
            old_string="Original body.", new_string="Patched body."))
    assert raw["success"] is True and raw["_forbidden"] is True
    assert "Запись запрещена, предложение сохранено в" in raw["message"]
    dest = Path(raw["need2write"])
    assert dest == n2w_env["n2w"] / "test-skill" / "SKILL.md"
    dumped = dest.read_text(encoding="utf-8")
    assert dumped == PATCHED                      # full final text, not a diff
    assert dumped.startswith("---\n")             # frontmatter intact -> copy-paste apply
    assert tree_bytes(n2w_env["skills"]) == before  # library untouched, byte for byte


def test_create_leaves_no_dir_and_dumps_skill_md(n2w_env):
    n2w_env["set_forbidden"](True)
    before = tree_bytes(n2w_env["skills"])
    with n2w_env["dirs"]():
        res = _create_skill("ghost-skill", VALID.replace("test-skill", "ghost-skill"))
    assert res["success"] and res["_forbidden"]
    assert _find_skill("ghost-skill") is None
    assert tree_bytes(n2w_env["skills"]) == before  # not even an empty dir appeared
    assert (n2w_env["n2w"] / "ghost-skill" / "SKILL.md").read_text(
        encoding="utf-8") == VALID.replace("test-skill", "ghost-skill")


def test_write_file_lands_next_to_proposal_for_uncreated_skill(n2w_env):
    """Batch shape: create only proposed, the skill never hit disk — write_file must not
    error the batch, its content lands in need2write under <skill>/<file_path>."""
    n2w_env["set_forbidden"](True)
    with n2w_env["dirs"]():
        raw = json.loads(skill_manage(action="write_file", name="test-skill",
                                      file_path="references/api.md", file_content="# API\n"))
        assert raw["success"] and raw["_forbidden"]
        assert raw["need2write"].endswith("test-skill/references/api.md")
        assert (n2w_env["n2w"] / "test-skill" / "references" / "api.md").read_text(
            encoding="utf-8") == "# API\n"


def test_delete_dumps_empty_marker_library_stays(n2w_env):
    n2w_env["set_forbidden"](True)
    before = tree_bytes(n2w_env["skills"])
    with n2w_env["dirs"]():
        raw = json.loads(skill_manage(action="delete", name="test-skill", absorbed_into=""))
    assert raw["success"] and raw["_forbidden"]
    assert (n2w_env["skills"] / "test-skill" / "SKILL.md").is_file()
    assert tree_bytes(n2w_env["skills"]) == before
    marker = n2w_env["n2w"] / "test-skill__del.md"
    assert marker.is_file() and marker.read_bytes() == b""


def test_remove_file_marker(n2w_env):
    n2w_env["set_forbidden"](True)
    ref = n2w_env["skills"] / "test-skill" / "references"
    ref.mkdir()
    (ref / "old.md").write_text("x", encoding="utf-8")
    with n2w_env["dirs"]():
        raw = json.loads(skill_manage(action="remove_file", name="test-skill",
                                      file_path="references/old.md"))
    assert raw["success"] and raw["_forbidden"]
    assert (ref / "old.md").is_file()
    assert (n2w_env["n2w"] / "test-skill" / "references" / "old.md__del").is_file()


def test_collision_gets_suffix_never_clobbers(n2w_env):
    """Double run of one cause: the second file must not overwrite the first (__2)."""
    n2w_env["set_forbidden"](True)
    with n2w_env["dirs"]():
        for _ in range(2):
            skill_manage(action="patch", name="test-skill",
                         old_string="Original body.", new_string="Patched body.")
    files = sorted(p.name for p in (n2w_env["n2w"] / "test-skill").iterdir())
    assert files == ["SKILL.md", "SKILL__2.md"]
    for name in files:
        assert (n2w_env["n2w"] / "test-skill" / name).read_text(encoding="utf-8") == PATCHED


def test_batch_rows_expose_the_proposal_path(n2w_env):
    n2w_env["set_forbidden"](True)
    with n2w_env["dirs"]():
        raw = json.loads(skill_manage(
            action="patch", name="test-skill", operations=[
                {"name": "test-skill", "action": "patch",
                 "old_string": "Original body.", "new_string": "Patched body."}]))
    assert raw["success"]
    row = raw["results"][0]
    assert "need2write" in row["need2write"] and "Запись запрещена" in row["message"]


# --- forbidden=true: the curator surface (acceptance #2) -------------------------

def test_curator_transitions_leave_tree_byte_identical(n2w_env, monkeypatch):
    """stale/archive passes must not change ~/.hermes/skills/** by a single byte
    (locks dir aside); every intended write shows up in need2write."""
    skills = n2w_env["skills"]
    usage = skills / ".usage.json"
    usage.write_text(json.dumps({"test-skill": {
        "created_by": "agent", "use_count": 1, "last_used_at": "2020-01-01T00:00:00+00:00",
        "created_at": "2020-01-01T00:00:00+00:00", "state": "active", "pinned": False,
    }}), encoding="utf-8")
    before = tree_bytes(skills)
    n2w_env["set_forbidden"](True)

    import importlib
    import tools.skill_usage as su
    importlib.reload(su)
    from agent import curator
    importlib.reload(curator)
    monkeypatch.setattr(curator, "_load_config", dict)

    counts = curator.apply_automatic_transitions()
    assert counts["archived"] >= 1  # the pass THINK it archived...
    # ...but the library is byte-for-byte what it was (transient lock files aside).
    after = {k: v for k, v in tree_bytes(skills).items() if not k.startswith(".locks")}
    assert after == before
    marker = n2w_env["n2w"] / "test-skill__archive.md"
    assert marker.is_file() and marker.read_bytes() == b""
    assert usage.read_text(encoding="utf-8") != ""  # .usage.json still the original
    # restore the shared modules to the ambient HERMES_HOME view for later tests
    importlib.reload(su)
    importlib.reload(curator)


def test_archive_restore_marker_names_are_self_describing(n2w_env, tmp_path):
    n2w_env["set_forbidden"](True)
    dest = skill_write_policy.dump("some-skill__archive.md", None)
    assert dest == n2w_env["n2w"] / "some-skill__archive.md"
    assert not dest.read_bytes()

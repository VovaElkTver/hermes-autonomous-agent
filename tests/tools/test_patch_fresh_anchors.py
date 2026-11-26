"""S5 (TZ-memdoor-5090-v2) acceptance: fresh anchors after an edit.

OFF by default the patch diff payload is byte-for-byte the current unified diff;
ON the payload is extended with per changed-region windows of the new lines ±3,
numbered — fresh anchors for the next edit without a re-read.
"""

import os

import pytest

from tools.patch_parser import (
    _fresh_anchors_block,
    _unified_diff,
    apply_v4a_operations,
    fresh_anchors_enabled,
    parse_v4a_patch,
)
import tools.patch_parser as pp


class _FakeRead:
    def __init__(self, content, error=None, sha=None):
        self.content = content
        self.error = error
        self._content_sha256 = sha


class _FakeWrite:
    def __init__(self):
        self.error = None
        self._content_sha256 = "w"
        self.lsp_diagnostics = None
        self.lint = None


class FakeFileOps:
    def __init__(self, files):
        self.files = dict(files)

    def read_file_raw(self, path):
        if path in self.files:
            return _FakeRead(self.files[path])
        return _FakeRead("", error=f"not found: {path}")

    def write_file(self, path, content, **extra):
        self.files[path] = content
        return _FakeWrite()


PATCH = """\
*** Begin Patch
*** Update File: src/main.py
@@ line10 @@
 line9
-line10
+LINE10
 line11
*** End Patch"""

CONTENT = "\n".join(f"line{i}" for i in range(1, 21))  # 20 lines, "line10" at line 10


@pytest.fixture(autouse=True)
def _isolate_anchors_state():
    pp._clear_cache()
    saved = os.environ.get("HERMES_FRESH_ANCHORS")
    os.environ.pop("HERMES_FRESH_ANCHORS", None)
    yield
    pp._clear_cache()
    if saved is None:
        os.environ.pop("HERMES_FRESH_ANCHORS", None)
    else:
        os.environ["HERMES_FRESH_ANCHORS"] = saved


def _apply(flag_value):
    """Run the patch with the fresh-anchors flag forced to ``flag_value``; return (diff, new_content)."""
    ops, err = parse_v4a_patch(PATCH)
    assert err is None
    fops = FakeFileOps({"src/main.py": CONTENT})
    os.environ["HERMES_FRESH_ANCHORS"] = "1" if flag_value else "0"
    pp._clear_cache()
    result = apply_v4a_operations(ops, fops)
    assert result.success, result.error
    assert fops.files["src/main.py"].split("\n")[9] == "LINE10"
    return result.diff, fops.files["src/main.py"]


class TestFreshAnchorsBlock:
    def test_numbered_windows(self):
        new_content = CONTENT.replace("line10", "LINE10", 1)
        diff = _unified_diff("src/main.py", CONTENT, new_content)
        block = _fresh_anchors_block("src/main.py", new_content, diff)
        assert block
        assert "[src/main.py]" in block
        # a 3-line-context hunk around line 10 of a 20-line file spans L7..L13
        assert "L7..L13" in block
        assert "    10| LINE10" in block   # the changed line, numbered
        assert "     7| line7" in block     # context above
        assert "    13| line13" in block    # context below

    def test_no_hunks_no_block(self):
        # an empty diff has no @@ headers -> no block
        assert _fresh_anchors_block("f.py", "x\ny\nz", "") == ""


class TestApplyPayload:
    def test_off_payload_is_plain_diff(self):
        diff, new_content = _apply(False)
        # OFF: the payload is exactly the current unified diff, no anchor extension
        assert "new lines L" not in diff
        assert diff == _unified_diff("src/main.py", CONTENT, new_content)
        assert "LINE10" in diff  # the change itself is still reported

    def test_on_appends_anchors(self):
        diff, new_content = _apply(True)
        plain = _unified_diff("src/main.py", CONTENT, new_content)
        block = _fresh_anchors_block("src/main.py", new_content, plain)
        assert diff == plain + "\n" + block
        assert "LINE10" in block  # the fresh anchor is the changed line, numbered

    def test_default_is_off(self):
        assert fresh_anchors_enabled() is False

"""Evidence-pin contract: cited inbound media survives the age-based cache sweep.

2026-11-03 incident: the agent claimed something about a screenshot, got corrected,
and could not re-check because the cached file had been swept. These tests pin the
mechanism that makes re-checking possible:

- a pinned file is skipped by the per-kind ``cleanup_*_cache`` sweep however old its mtime;
- the rolling TTL extends on re-pin, bounded by an absolute cap from FIRST pin;
- ``prune_expired`` drops expired entries and pins of vanished files;
- a corrupt registry degrades to "no pins" — deletion never blocks on it.
"""

import json
import os
import time
from pathlib import Path

import pytest

from gateway.platforms import media_pin
from gateway.platforms.base import cleanup_image_cache, cleanup_document_cache


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_MEDIA_PINS", raising=False)
    return tmp_path


def _backdated_file(root: Path, name: str, age_hours: float) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    f = root / name
    f.write_bytes(b"x")
    old = time.time() - age_hours * 3600
    os.utime(f, (old, old))
    return f


def test_pinned_file_survives_age_sweep(isolated_home):
    cache = isolated_home / "cache" / "images"
    pinned = _backdated_file(cache, "pinned.jpg", 48)
    plain = _backdated_file(cache, "plain.jpg", 48)
    assert media_pin.pin_media_file(pinned) is True

    removed = cleanup_image_cache(max_age_hours=24)

    assert removed == 1
    assert pinned.exists()
    assert not plain.exists()


def test_rolling_ttl_extends_but_absolute_cap_holds(isolated_home, tmp_path):
    target = _backdated_file(tmp_path, "evidence.jpg", 0)
    store = tmp_path / "store.json"

    t_first = 1_000_000.0
    media_pin.pin_media_file(target, now_ts=t_first, ttl_hours=2, absolute_hours=10,
                             _path_override=store)
    key = str(Path(target).resolve())
    entry = json.loads(store.read_text())[key]
    assert entry["born"] == t_first
    assert entry["until"] == t_first + 2 * 3600

    # Re-pin far in the future: rolling window moves, the absolute cap anchors at first pin.
    t_second = t_first + 9 * 3600
    media_pin.pin_media_file(target, now_ts=t_second, ttl_hours=50, absolute_hours=10,
                             _path_override=store)
    entry = json.loads(store.read_text())[key]
    assert entry["until"] == t_first + 10 * 3600  # born + absolute wins over now + ttl

    # Idempotence: same state does not rewrite.
    before = store.read_text()
    changed = media_pin.pin_media_file(target, now_ts=t_second, ttl_hours=50,
                                       absolute_hours=10, _path_override=store)
    assert changed is False
    assert store.read_text() == before


def test_prune_expired_and_missing(isolated_home, tmp_path):
    live_file = _backdated_file(tmp_path, "live.bin", 0)
    gone_file = tmp_path / "gone.bin"
    store = tmp_path / "store.json"
    now = time.time()
    for p, offset in ((str(Path(live_file).resolve()), 1.0), (str(gone_file), 1.0)):
        media_pin.pin_media_file(p, now_ts=now - offset * 3600, ttl_hours=2,
                                 _path_override=store)
    # Expire the second pin by shrinking its window via a direct rewrite.
    data = json.loads(store.read_text())
    key_gone = str(gone_file.resolve())
    data[key_gone]["until"] = now - 1
    store.write_text(json.dumps(data))

    removed = media_pin.prune_expired(_path_override=store)

    assert removed == 1
    kept = json.loads(store.read_text())
    assert set(kept) == {str(Path(live_file).resolve())}


def test_corrupt_store_degrades_to_no_pins(isolated_home, tmp_path):
    cache = isolated_home / "cache" / "documents"
    f = _backdated_file(cache, "doc.pdf", 48)
    (isolated_home / "cache" / "media_pins.json").write_text("{{{not json")

    removed = cleanup_document_cache(max_age_hours=24)

    assert removed == 1
    assert not f.exists()

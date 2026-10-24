"""Matrix media-filename heuristics.

Extracted from ``adapter.py`` to keep that module inside its FILE_LINES cap
(moved code keeps its cap): deciding whether a media event body is a bare
uploaded filename rather than user text.
"""
from __future__ import annotations

import mimetypes
from pathlib import Path

__all__ = [
    "_is_bare_media_filename",
    "_looks_like_matrix_image_filename",
    "_looks_like_matrix_media_filename",
    "_looks_like_transport_filename",
]

_MATRIX_IMAGE_FILENAME_EXTS = frozenset({
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".heic", ".heif", ".avif"})
_MATRIX_MEDIA_FILENAME_EXTS = frozenset({
    ".ogg", ".oga", ".opus", ".m4a", ".mp3", ".wav", ".flac", ".aac", ".amr", ".mp4", ".webm", ".mov", ".mkv"})


def _looks_like_matrix_image_filename(text: str) -> bool:
    """True when an m.image body is just the uploaded filename (no caption) — not user text."""
    return _looks_like_transport_filename(text, "image/", _MATRIX_IMAGE_FILENAME_EXTS)


def _looks_like_transport_filename(text: str, mime_prefixes, exts: frozenset, reject_spaces: bool = False) -> bool:
    """Bare single-token filename with a known media extension or a matching guessed MIME type."""
    candidate = str(text or "").strip()
    if not candidate or "\n" in candidate or candidate.endswith("/"):
        return False
    # A genuine caption essentially always contains whitespace; a bare transport filename does not.
    if reject_spaces and any(ch.isspace() for ch in candidate):
        return False
    if Path(candidate).name != candidate:
        return False
    suffix = Path(candidate).suffix.lower()
    if not suffix:
        return False
    guessed_type, _ = mimetypes.guess_type(candidate)
    return bool(guessed_type and guessed_type.startswith(mime_prefixes)) or suffix in exts


def _looks_like_matrix_media_filename(text: str) -> bool:
    """True when an m.audio/m.file/m.video body is just the uploaded filename (no caption)."""
    return _looks_like_transport_filename(text, ("audio/", "video/"), _MATRIX_MEDIA_FILENAME_EXTS, True)


def _is_bare_media_filename(msgtype: str, body: str) -> bool:
    """True when a media event body is only the uploaded filename for its msgtype."""
    if msgtype == "m.image":
        return _looks_like_matrix_image_filename(body)
    return msgtype in ("m.audio", "m.file", "m.video") and _looks_like_matrix_media_filename(body)

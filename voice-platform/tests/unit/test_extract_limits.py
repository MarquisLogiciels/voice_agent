"""Audit F4: document extraction is bounded (zip bombs, size, time) and off the event loop."""

from __future__ import annotations

import io
import zipfile

import pytest

from voice_platform.knowledge.extract import (
    _DOCX,
    extract_text,
    extract_text_isolated,
)
from voice_platform.shared.errors import ValidationFailedError


def _zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return buf.getvalue()


def test_docx_zip_bomb_is_refused_before_parsing() -> None:
    bomb = _zip({"word/document.xml": b"\0" * (120 * 1024 * 1024)})  # ~120 KB on disk
    assert len(bomb) < 1024 * 1024
    with pytest.raises(ValidationFailedError, match="unreasonable size"):
        extract_text(bomb, _DOCX)


def test_docx_with_too_many_entries_is_refused() -> None:
    many = _zip({f"f{i}.xml": b"x" for i in range(2_100)})
    with pytest.raises(ValidationFailedError, match="unreasonable size"):
        extract_text(many, _DOCX)


async def test_isolated_extraction_returns_text_and_errors() -> None:
    assert await extract_text_isolated(b"Hours: 9 to 5.", "text/plain") == "Hours: 9 to 5."
    with pytest.raises(ValidationFailedError, match="no extractable text"):
        await extract_text_isolated(b"   ", "text/plain")


async def test_isolated_extraction_is_killed_on_timeout() -> None:
    with pytest.raises(ValidationFailedError, match="took longer"):
        await extract_text_isolated(b"x" * 1000, "text/plain", timeout_s=0.001)

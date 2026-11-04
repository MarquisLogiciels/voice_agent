"""Text extraction for uploaded business documents (PDF, DOCX, HTML, text)."""

from __future__ import annotations

import io
import re
import unicodedata
from typing import Any

from ..shared.errors import ValidationFailedError

MAX_CHARS = 2_000_000
MAX_PDF_PAGES = 500
# DOCX is a zip archive: bound what decompression may expand to (zip bombs)
MAX_DOCX_UNCOMPRESSED = 100 * 1024 * 1024
MAX_DOCX_RATIO = 200
MAX_DOCX_ENTRIES = 2_000
EXTRACT_TIMEOUT_S = 60.0

SUPPORTED_TYPES = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "text/html": "html",
    "text/plain": "text",
    "text/markdown": "text",
    "text/csv": "text",
    "application/json": "text",
}

_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_EXT = {
    ".pdf": "application/pdf",
    ".docx": _DOCX,
    ".html": "text/html",
    ".htm": "text/html",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".json": "application/json",
}


def sniff_mime(filename: str, declared: str | None, data: bytes) -> str:
    if data[:5] == b"%PDF-":
        return "application/pdf"
    if data[:2] == b"PK" and filename.lower().endswith(".docx"):
        return _EXT[".docx"]
    if declared and declared.split(";")[0].strip() in SUPPORTED_TYPES:
        return declared.split(";")[0].strip()
    for ext, mime in _EXT.items():
        if filename.lower().endswith(ext):
            return mime
    raise ValidationFailedError(
        "unsupported document type", details={"supported": sorted(SUPPORTED_TYPES)}
    )


def _clean(text: str) -> str:
    text = unicodedata.normalize("NFC", text).replace("\x00", "")
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:MAX_CHARS]


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def extract_text(data: bytes, mime: str) -> str:
    kind = SUPPORTED_TYPES.get(mime)
    if kind is None:
        raise ValidationFailedError(f"unsupported document type {mime}")
    try:
        if kind == "pdf":
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted:
                raise ValidationFailedError("encrypted PDFs are not supported")
            pages = [p.extract_text() or "" for p in reader.pages[:MAX_PDF_PAGES]]
            text = "\n\n".join(pages)
        elif kind == "docx":
            import docx

            _check_zip(data)
            document = docx.Document(io.BytesIO(data))
            parts: list[str] = []
            for para in document.paragraphs:
                if para.text.strip():
                    style = (para.style.name or "").lower() if para.style is not None else ""
                    parts.append(("## " if style.startswith("heading") else "") + para.text)
            for table in document.tables:
                for row in table.rows:
                    parts.append(" | ".join(c.text.strip() for c in row.cells))
            text = "\n\n".join(parts)
        elif kind == "html":
            text = html_to_text(_decode(data))
        else:
            text = _decode(data)
    except ValidationFailedError:
        raise
    except Exception as e:
        raise ValidationFailedError(f"could not extract text: {type(e).__name__}") from e
    if len(text) > MAX_CHARS * 2:
        raise ValidationFailedError("document is too large to index")
    cleaned = _clean(text)[:MAX_CHARS]
    if not cleaned:
        raise ValidationFailedError("document contains no extractable text (scanned PDF?)")
    return cleaned


def _check_zip(data: bytes) -> None:
    import zipfile

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            infos = z.infolist()
    except zipfile.BadZipFile as e:
        raise ValidationFailedError("not a valid DOCX file") from e
    total = sum(i.file_size for i in infos)
    if (
        len(infos) > MAX_DOCX_ENTRIES
        or total > MAX_DOCX_UNCOMPRESSED
        or total > MAX_DOCX_RATIO * max(len(data), 1)
    ):
        raise ValidationFailedError("DOCX expands to an unreasonable size")


def _extract_child(data: bytes, mime: str, conn: Any) -> None:
    try:
        conn.send(("ok", extract_text(data, mime)))
    except ValidationFailedError as e:
        conn.send(("invalid", e.message))
    except Exception as e:  # pragma: no cover - extract_text wraps parser errors
        conn.send(("invalid", f"could not extract text: {type(e).__name__}"))
    finally:
        conn.close()


async def extract_text_isolated(
    data: bytes, mime: str, *, timeout_s: float = EXTRACT_TIMEOUT_S
) -> str:
    """Run ``extract_text`` in a separate process with a hard time limit.

    Parsers are CPU-bound and attacker-influenced (uploaded files): running them on the job
    runner's event loop blocked it, and a thread cannot be stopped when a crafted PDF makes the
    parser spin. The child is killed on timeout.
    """
    import asyncio
    import multiprocessing

    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_extract_child, args=(data, mime, child), daemon=True)
    proc.start()
    child.close()
    try:
        ready = await asyncio.to_thread(parent.poll, timeout_s)
        if not ready:
            raise ValidationFailedError(f"document took longer than {timeout_s:.0f}s to process")
        status, payload = parent.recv()
    except EOFError as e:
        raise ValidationFailedError("document could not be processed") from e
    finally:
        if proc.is_alive():
            proc.kill()
        await asyncio.to_thread(proc.join, 5)
        parent.close()
    if status != "ok":
        raise ValidationFailedError(payload)
    return str(payload)


def html_to_text(html: str) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "form", "svg"]):
        tag.decompose()
    lines: list[str] = []
    for el in soup.find_all(["h1", "h2", "h3", "h4", "p", "li", "td", "th", "dt", "dd"]):
        text = el.get_text(" ", strip=True)
        if not text:
            continue
        if el.name in ("h1", "h2", "h3", "h4"):
            lines.append("## " + text)
        else:
            lines.append(text)
    if not lines:
        lines = [soup.get_text("\n", strip=True)]
    return "\n\n".join(lines)

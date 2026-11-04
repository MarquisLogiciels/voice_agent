"""Text extraction for uploaded business documents (PDF, DOCX, HTML, text)."""

from __future__ import annotations

import io
import re
import unicodedata

from ..shared.errors import ValidationFailedError

MAX_CHARS = 2_000_000
MAX_PDF_PAGES = 500

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
_EXT = {".pdf": "application/pdf", ".docx": _DOCX,
        ".html": "text/html", ".htm": "text/html", ".txt": "text/plain", ".md": "text/markdown",
        ".csv": "text/csv", ".json": "application/json"}


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
    raise ValidationFailedError("unsupported document type",
                                details={"supported": sorted(SUPPORTED_TYPES)})


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
    cleaned = _clean(text)
    if not cleaned:
        raise ValidationFailedError("document contains no extractable text (scanned PDF?)")
    return cleaned


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

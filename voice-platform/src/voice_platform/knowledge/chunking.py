"""Structure-aware chunking: split on headings, then paragraphs, then sentences."""

from __future__ import annotations

import re
from dataclasses import dataclass

_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-ZÀ-Ý0-9«\"'])")


@dataclass(frozen=True)
class Chunk:
    ordinal: int
    content: str
    heading: str | None


def _split_long(text: str, size: int) -> list[str]:
    if len(text) <= size:
        return [text]
    sentences = _SENT.split(text)
    out: list[str] = []
    cur = ""
    for s in sentences:
        if len(s) > size:  # pathological: hard wrap on whitespace
            words = s.split()
            for w in words:
                if len(cur) + len(w) + 1 > size and cur:
                    out.append(cur)
                    cur = ""
                cur = f"{cur} {w}".strip()
            continue
        if len(cur) + len(s) + 1 > size and cur:
            out.append(cur)
            cur = ""
        cur = f"{cur} {s}".strip()
    if cur:
        out.append(cur)
    return out


def chunk_text(text: str, *, size: int = 900, overlap: int = 150) -> list[Chunk]:
    if size < 200:
        raise ValueError("chunk size too small")
    overlap = max(0, min(overlap, size // 2))
    sections: list[tuple[str | None, str]] = []
    heading: str | None = None
    buf: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        first, _, rest = para.partition("\n")
        if first.startswith(("# ", "## ", "### ")) or (len(para) < 80 and para.isupper()):
            if buf:
                sections.append((heading, "\n\n".join(buf)))
                buf = []
            heading = first.lstrip("#").strip() if first.startswith("#") else first.strip()
            # text directly under the heading (single newline) belongs to the section body
            if rest.strip():
                buf.append(rest.strip())
            continue
        buf.append(para)
    if buf:
        sections.append((heading, "\n\n".join(buf)))

    chunks: list[Chunk] = []
    for head, body in sections:
        pieces: list[str] = []
        cur = ""
        for para in body.split("\n\n"):
            for piece in _split_long(para, size):
                if len(cur) + len(piece) + 2 > size and cur:
                    pieces.append(cur)
                    tail = cur[-overlap:] if overlap else ""
                    # start the overlap at a word boundary
                    tail = tail[tail.find(" ") + 1 :] if " " in tail else tail
                    joined = (tail + " " + piece).strip() if tail else piece
                    cur = joined if len(joined) <= size else piece
                else:
                    cur = f"{cur}\n\n{piece}".strip()
        if cur:
            pieces.append(cur)
        for p in pieces:
            content = f"{head}\n{p}" if head else p
            chunks.append(Chunk(ordinal=len(chunks), content=content, heading=head))
    return chunks

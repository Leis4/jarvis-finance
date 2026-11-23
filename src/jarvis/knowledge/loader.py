"""Document loader for TXT, MD, PDF, EPUB files."""
import hashlib
import json
from importlib.metadata import version
from io import BytesIO
from pathlib import Path
from typing import Any

from jarvis.knowledge.chunker import chunk_text
from jarvis.knowledge.errors import KnowledgeParseError


def load_pdf_evidence(path: Path, *, chunk_size: int = 500, overlap: int = 50) -> dict[str, Any]:
    """Bind page-local chunks to original PDF bytes without inventing citations."""
    chunk_text("validation", chunk_size, overlap)
    if path.suffix.lower() != ".pdf":
        raise KnowledgeParseError("PDF source required")
    try:
        raw = path.read_bytes()
        result = _pdf_evidence(raw, path.resolve().as_uri(), chunk_size, overlap)
        if path.read_bytes() != raw:
            raise KnowledgeParseError("PDF empty or original changed while parsing")
        return result
    except OSError:
        raise KnowledgeParseError("PDF original unavailable") from None


def load_pdf_bytes_evidence(raw: bytes, *, chunk_size: int = 500, overlap: int = 50) -> dict[str, Any]:
    """Parse bounded inline originals with content identity and no filesystem URI."""
    chunk_text("validation", chunk_size, overlap)
    if (not isinstance(raw, bytes) or not 0 < len(raw) <= 8 * 1024 * 1024
            or not raw.startswith(b"%PDF-") or chunk_size > 2000):
        raise KnowledgeParseError("Bounded PDF original required")
    return _pdf_evidence(raw, "urn:sha256:" + hashlib.sha256(raw).hexdigest(), chunk_size, overlap)


def _pdf_evidence(raw: bytes, source_uri: str, chunk_size: int, overlap: int) -> dict[str, Any]:
    """Shared page parser for file and inline originals."""
    try:
        import pypdf

        reader = pypdf.PdfReader(BytesIO(raw))
        if source_uri.startswith("urn:") and len(reader.pages) > 200:
            raise KnowledgeParseError("PDF page limit exceeded")
        pages: list[dict[str, Any]] = []
        chunks: list[dict[str, Any]] = []
        for number, page in enumerate(reader.pages, 1):
            text = page.extract_text()
            if source_uri.startswith("urn:") and text and len(text) > 100000:
                raise KnowledgeParseError("PDF page text limit exceeded")
            if not text or not text.strip():
                raise KnowledgeParseError("Page text unavailable; complete extraction or OCR required")
            page_hash = hashlib.sha256(text.encode()).hexdigest()
            pages.append({"page": number, "text": text, "text_sha256": page_hash})
            for index, chunk in enumerate(chunk_text(text, chunk_size, overlap)):
                chunks.append({"page": number, "page_chunk_index": index, "chunk_index": len(chunks),
                    "text": chunk, "chunk_sha256": hashlib.sha256(chunk.encode()).hexdigest(),
                    "page_text_sha256": page_hash, "citation_scope": "page"})
        if not pages:
            raise KnowledgeParseError("PDF empty or original changed while parsing")
        evidence = {"format": "jarvis-pdf-lineage-v1", "source_uri": source_uri,
            "source_sha256": hashlib.sha256(raw).hexdigest(), "parser_version": "pypdf:" + version("pypdf") + ";pages-v1",
            "chunker_version": "words-v1", "chunk_size": chunk_size, "overlap": overlap,
            "page_count": len(pages), "pages": pages, "chunks": chunks,
            "freshness": "unverified", "database_modified": False, "indexed": False}
        return {**evidence, "lineage_sha256": hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()}
    except KnowledgeParseError:
        raise
    except Exception:  # noqa: BLE001 - original/parser boundary
        raise KnowledgeParseError("PDF page extraction unavailable") from None


def load_file(path: Path) -> str:
    """Load text without silently accepting corruption or unavailable OCR."""
    try:
        text = _load_file(path)
        if not text.strip():
            raise KnowledgeParseError("Document text unavailable")
        return text
    except (OSError, UnicodeError):
        raise KnowledgeParseError("Document unreadable or invalid UTF-8") from None


def _load_file(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in ('.txt', '.md'):
        return path.read_text(encoding='utf-8', errors='strict')
    if suffix == '.pdf':
        evidence = load_pdf_evidence(path)
        return '\n'.join(page["text"] for page in evidence["pages"])
    if suffix == '.epub':
        try:
            import ebooklib  # type: ignore[import-untyped]
            from bs4 import BeautifulSoup  # type: ignore[import-untyped]
            from ebooklib import epub  # type: ignore[import-untyped]

            book = epub.read_epub(path)
            texts = []
            for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
                soup = BeautifulSoup(item.get_content(), 'html.parser')
                texts.append(soup.get_text())
            text = '\n'.join(texts)
            if not text.strip():
                raise KnowledgeParseError('EPUB text unavailable')
            return text
        except Exception:  # noqa: BLE001 - document parser boundary
            raise KnowledgeParseError('Document parsing unavailable') from None
    raise KnowledgeParseError('Unsupported document format')


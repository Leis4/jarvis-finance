"""Explicit knowledge ingestion and retrieval failures."""


class KnowledgeUnavailable(RuntimeError):
    """Backend/model/collection cannot produce trustworthy retrieval."""


class KnowledgeStale(KnowledgeUnavailable):
    """Retrieved evidence exceeds the explicitly requested observation age."""

    status = "stale"


class KnowledgeParseError(ValueError):
    """Document is unsupported, corrupt or needs unavailable OCR."""

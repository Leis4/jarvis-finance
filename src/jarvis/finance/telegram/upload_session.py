"""Track upload sessions per user — keyed by PdfType, not count."""
from dataclasses import dataclass, field
from datetime import UTC, datetime

from jarvis.finance.telegram.pdf_classifier import ClassifiedPdf, PdfType


@dataclass
class UploadSession:
    user_id: int
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    files: dict[PdfType, ClassifiedPdf] = field(default_factory=dict)

    @property
    def count(self) -> int:
        return len(self.files)

    @property
    def missing(self) -> list[PdfType]:
        expected = {PdfType.SAVINGS, PdfType.MAIN, PdfType.RESERVE,
                    PdfType.BALANCES if PdfType.ADDITIONAL not in self.files else PdfType.ADDITIONAL}
        return sorted([t for t in expected if t not in self.files], key=lambda t: t.value)

    @property
    def is_complete(self) -> bool:
        return self.count == 4


_sessions: dict[int, UploadSession] = {}


def get_or_create(user_id: int) -> UploadSession:
    if user_id not in _sessions:
        _sessions[user_id] = UploadSession(user_id=user_id)
    return _sessions[user_id]


def get(user_id: int) -> UploadSession | None:
    return _sessions.get(user_id)


def clear(user_id: int) -> None:
    _sessions.pop(user_id, None)

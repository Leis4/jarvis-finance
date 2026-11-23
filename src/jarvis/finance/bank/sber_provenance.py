"""Versioned original-row evidence sidecar; legacy parser and financial IDs stay intact."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from jarvis.finance.bank.sber_statement import (
    PDF_DETAIL_RE,
    PDF_MONEY_AT_END_RE,
    _match_pdf_tx_line,
    parse_sber_pdf_text,
)
from jarvis.finance.ledger import transaction_payload
from jarvis.finance.sber_intake import digest
from jarvis.finance.statement_product import parser_version

VERSION = 'sber-original-row-preview-v2'


def text_provenance(pages: list[str], *, source_sha256: str, account: str) -> dict[str, Any]:
    """Bind existing parsed transactions to independently retained source spans."""
    if not pages or len(pages) > 200 or sum(len(p) for p in pages) > 2_000_000:
        raise ValueError('Bounded text pages required')
    if len(source_sha256) != 64 or any(c not in '0123456789abcdef' for c in source_sha256):
        raise ValueError('SHA256 required')
    text = '\n'.join(pages)
    raw_lines = text.splitlines(keepends=True)
    lines = [' '.join(line.replace('\xa0', ' ').split()) for line in raw_lines]
    parsed = parse_sber_pdf_text(text, account=account)
    whole = [transaction_payload(tx) for tx in parsed.transactions]
    if len(whole) > 10000:
        raise ValueError('Bounded original history required')
    savings = any('сберегательный счет' in line.lower() for line in lines)
    if savings:
        starts = [i for i, line in enumerate(lines) if re.match(r'^\d{2}\.\d{2}\.\d{4}\s+(?!\d{2}:\d{2}\s)', line)]
    else:
        starts = [i for i, line in enumerate(lines) if _match_pdf_tx_line(line)]
    page_starts = []
    position = 0
    for page in pages:
        page_starts.append(position)
        position += len(page) + 1
    offsets = [0]
    for line in raw_lines:
        offsets.append(offsets[-1] + len(line))
    evidence: list[dict[str, Any]] = []
    for start, end in zip(starts, starts[1:] + [len(lines)] if starts else [], strict=True):
        chunk = lines[start:end]
        candidate = parse_sber_pdf_text(('Сберегательный счет\n' if savings else '') + '\n'.join(chunk), account=account).transactions
        ordinal = len(evidence)
        if len(candidate) != 1 or ordinal >= len(whole) or transaction_payload(candidate[0]) != whole[ordinal]:
            # No alignment by amount, guessed ordinal or duplicate payload search.
            break
        head = _match_pdf_tx_line(lines[start])
        if not savings:
            assert head is not None
        detail = next((PDF_DETAIL_RE.match(line) for line in chunk[1:] if PDF_DETAIL_RE.match(line)), None)
        stop = start + chunk.index(detail.string) + 1 if detail else end
        if savings:
            amounts = next((n for n in range(start + 1, end)
                if len(list(PDF_MONEY_AT_END_RE.finditer(lines[n]))) >= 2), None)
            if amounts is None:
                break
            stop = amounts + 1
        span = ''.join(raw_lines[start:stop])
        page_number = max(n for n, pos in enumerate(page_starts) if pos <= offsets[start]) + 1
        evidence.append({'ordinal': ordinal, 'page': page_number, 'line': start + 1, 'end_line': stop,
            'operation_date': candidate[0].tx_date.isoformat(), 'operation_time': head['time'] if head else None,
            'processing_date': detail.group('date') if detail else None,
            'authorization_code': detail.group('auth') if detail else None,
            'original_span_sha256': hashlib.sha256(span.encode()).hexdigest(),
            'parsed_transaction_sha256': digest(whole[ordinal]),
            'evidence_id': digest([VERSION, source_sha256, page_number, start + 1, stop, ordinal, span]),
            'economic_distinctness_proven': False})
    return {'version': VERSION, 'source_sha256': source_sha256, 'parser_version': parser_version(),
        'preview_implementation': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'status': 'bound' if len(evidence) == len(whole) and whole else 'partial', 'rows': evidence,
        'parsed_count': len(whole), 'unbound_count': len(whole) - len(evidence),
        'accepted_identity_changed': False, 'financial_apply_allowed': False, 'database_modified': False}


def pdf_provenance(path: Path, *, account: str) -> dict[str, Any]:
    """Read an immutable original; decline unsupported extraction instead of guessing."""
    from pypdf import PdfReader
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 20 * 1024 * 1024:
        raise ValueError('Bounded original PDF required')
    raw = path.read_bytes()
    reader = PdfReader(path)
    if len(reader.pages) > 200:
        raise ValueError('PDF page cap')
    result = text_provenance([p.extract_text() or '' for p in reader.pages],
        source_sha256=hashlib.sha256(raw).hexdigest(), account=account)
    if path.read_bytes() != raw:
        raise ValueError('Original changed during preview')
    return result

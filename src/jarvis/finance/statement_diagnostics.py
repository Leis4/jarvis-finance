"""Private PDF warning evidence without source acceptance or category decisions."""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

from jarvis.finance.bank.sber_statement import _extract_pdf_text
from jarvis.finance.ledger import finite_money
from jarvis.finance.production_intake import build_statement_preview
from jarvis.finance.sber_intake import digest

DATE = r"\d{2}\.\d{2}\.\d{4}"


def warning_evidence(warning: str, text: str, period: list[str] | None) -> dict[str, Any]:
    """Bind each diagnostic to its exact original line; unknown layouts remain unresolved."""
    match = re.fullmatch(r"line (\d+): skipped unrecognized Sber PDF row: (.*)", warning)
    result: dict[str, Any] = {"warning": warning, "classification": "unresolved", "line_verified": False,
        "source_acceptance_allowed": False, "financial_completeness_proven": False}
    if match is None:
        return result
    index, fragment = int(match[1]) - 1, match[2]
    lines = [" ".join(line.replace("\xa0", " ").strip().split()) for line in text.splitlines()]
    if not 0 <= index < len(lines) or lines[index][:120] != fragment:
        return result
    line = lines[index]
    result.update(line_verified=True, line_number=index + 1, line_sha256=digest(line),
                  original_line=line, context=lines[max(0, index - 2):index + 3])
    if re.fullmatch(rf"от {DATE}", line, re.IGNORECASE):
        result["classification"] = "document_date_header"
    elif re.fullmatch(rf"Дата открытия сч[её]та {DATE}", line, re.IGNORECASE):
        result["classification"] = "account_opening_date_header"
    elif re.fullmatch(rf"до {DATE}", line, re.IGNORECASE):
        result["classification"] = "valid_until_date_requires_context"
    elif period:
        dates = [".".join(reversed(day.split("-"))) for day in period]
        if line in {f"За период {dates[0]} — {dates[1]}", f"За период {dates[0]} - {dates[1]}",
                    f"с {dates[0]} по {dates[1]}", f"С {dates[0]} по {dates[1]}"}:
            result["classification"] = "declared_period_header"
        elif re.fullmatch(rf"[сС] {DATE} по {DATE}", line):
            result["classification"] = "related_period_not_statement_scope"
        elif re.fullmatch(DATE, line):
            # A detached date can be metadata OR part of an operation split over lines.
            result["classification"] = "detached_date_requires_layout_evidence"
    result["metadata_shape_proven"] = result["classification"] in {
        "document_date_header", "account_opening_date_header", "declared_period_header"}
    return result


def diagnose_pdf(path: Path, mapping: dict[str, str]) -> dict[str, Any]:
    """Return operator-private raw diagnostics and exact arithmetic, never a trust grant."""
    original = path.read_bytes()
    sha = hashlib.sha256(original).hexdigest()
    text = _extract_pdf_text(path)
    preview = build_statement_preview(path, mapping)
    metadata, rows = preview["metadata"], preview["transactions"]
    warnings = [warning_evidence(w, text, metadata.get("period")) for w in preview["parser"]["warnings"]]
    credits = sum((finite_money(r["amount"]) for r in rows if finite_money(r["amount"]) > 0), Decimal(0))
    debits = sum((-finite_money(r["amount"]) for r in rows if finite_money(r["amount"]) < 0), Decimal(0))
    opening = finite_money(metadata["opening"]) if metadata.get("opening") is not None else None
    closing = finite_money(metadata["closing"]) if metadata.get("closing") is not None else None
    discrepancy = closing - opening - credits + debits if opening is not None and closing is not None else None
    identities: dict[str, list[int]] = {}
    for ordinal, row in enumerate(rows):
        identities.setdefault(digest(row), []).append(ordinal)
    # Dates are not inferred from a matching net sum.
    scope_valid = bool(metadata.get("period") and metadata.get("local_account") and metadata.get("currency"))
    if scope_valid:
        scope_valid = all(metadata["period"][0] <= r["tx_date"] <= metadata["period"][1]
                          and r["currency"] == metadata["currency"] for r in rows)
    if path.read_bytes() != original:
        raise ValueError("Original changed during diagnostics")
    return {"source_sha256": sha, "parser_sha256": hashlib.sha256(
                Path(__file__).with_name("bank").joinpath("sber_statement.py").read_bytes()).hexdigest(),
        "metadata": metadata, "operation_count": len(rows), "count_basis": "parsed_rows_not_independent_bank_count",
        "credits": str(credits), "debits": str(debits),
        "discrepancy": str(discrepancy) if discrepancy is not None else None,
        "scope_valid": scope_valid, "preview_status": preview["status"], "issues": preview["issues"],
        "warnings": warnings, "warning_counts": dict(Counter(w["classification"] for w in warnings)),
        "potential_exact_duplicate_ordinals": [v for v in identities.values() if len(v) > 1],
        "financial_completeness_proven": False, "source_acceptance_allowed": False,
        "requires": ["detached_date_layout_or_statement_structure", "independent_history_count_or_MCP_comparison",
                     "owner_or_delegated_source_acceptance"],
        "database_modified": False, "original_modified": False}


def bound_warning_evidence(product: Any, source: dict[str, Any]) -> dict[str, Any]:
    """Verify private diagnostic artifacts, without turning explanations into consent."""
    from pypdf import PdfReader

    warnings = source['parser_warnings']
    if not warnings:
        return {'status': 'not_required', 'entries': [], 'binding': digest([])}
    try:
        folder = product._folder('statement-evidence')
        manifest_path = folder / 'warnings.json'
        if manifest_path.is_symlink() or manifest_path.stat().st_size > 256 * 1024:
            raise ValueError
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        packet = manifest[source['id']]
        rebound = False
        if source.get('accepted') and packet.get('revision') == source['revision']:
            events = product.store.audit()
            for start in events:
                if start.get('domain') != 'statement_group' or start.get('action') != 'start_group':
                    continue
                card_now: dict[str, Any] = next((c for c in start.get('source_preflight', []) if c['id'] == source['id']), {})
                finish_now: dict[str, Any] = next((e for e in events if e.get('domain') == 'statement_group'
                    and e.get('action') == 'finish_group' and e['item_id'] == start['item_id']), {})
                if (any(o['id'] == source['id'] and o.get('revision') == source['revision'] and o['status'] == 'accepted'
                        for o in finish_now.get('result', {}).get('sources', []))
                        and card_now.get('warning_evidence', {}).get('binding') != digest(packet)):
                    return {'status': 'unavailable', 'reason': 'ACCEPTED_DIAGNOSTIC_PACKET_CHANGED', 'binding': None, 'entries': []}
        if packet.get('revision') != source['revision']:
            # Derive a new binding from the actual audited approval, never edit the old packet.
            events = product.store.audit()
            decisions = [e for e in events if e.get('domain') == 'statement_source'
                         and e['item_id'] == 'statement:' + source['id']]
            acceptance = decisions[-1] if decisions else {}
            for event in events:
                if event.get('domain') != 'statement_group' or event.get('action') != 'start_group':
                    continue
                card: dict[str, Any] = next((c for c in event.get('source_preflight', []) if c['id'] == source['id']), {})
                finish: dict[str, Any] = next((e for e in events if e.get('domain') == 'statement_group'
                               and e.get('item_id') == event['item_id'] and e.get('action') == 'finish_group'), {})
                outcome: dict[str, Any] = next((o for o in finish.get('result', {}).get('sources', []) if o['id'] == source['id']), {})
                if (acceptance.get('action') == 'accept_statement' and acceptance.get('seq') == source['revision']
                        and acceptance.get('source_sha256') == source['sha256']
                        and acceptance.get('parser_warnings_acknowledged') is True
                        and (acceptance.get('supersedes') or 0) == packet.get('revision')
                        and card.get('revision') == packet.get('revision') and card.get('sha256') == source['sha256']
                        and card.get('parser_version') == source['parser_version']
                        and card.get('warning_evidence', {}).get('binding') == digest(packet)
                        and outcome.get('status') == 'accepted' and outcome.get('revision') == source['revision']
                        and event['seq'] < source['revision'] < finish.get('seq', 0)):
                    rebound = True
            if not rebound:
                return {'status': 'unavailable', 'reason': 'REVISION_TRANSITION_UNPROVEN', 'binding': None, 'entries': []}
        if (packet['source_id'] != source['id'] or packet['source_sha256'] != source['sha256']
                or packet['parser_version'] != source['parser_version'] or type(packet['revision']) is not int
                or (packet['revision'] != source['revision'] and not rebound) or len(packet['entries']) != len(warnings)):
            raise ValueError
        original = product._folder('statement-originals') / source['original']
        pages = [page.extract_text() or '' for page in PdfReader(original).pages]
        text = '\n'.join(pages)
        entries = []
        for warning, entry in zip(warnings, packet['entries'], strict=True):
            diagnostic = warning_evidence(warning, text, source['metadata']['period'])
            line = diagnostic.get('line_number')
            offset = sum(len(raw) for raw in text.splitlines(keepends=True)[:line - 1]) if line else -1
            page = max((n + 1 for n in range(len(pages))
                        if sum(len(p) + 1 for p in pages[:n]) <= offset), default=None)
            if (not diagnostic['line_verified'] or entry['warning_sha256'] != digest(warning)
                    or entry['line_sha256'] != diagnostic['line_sha256'] or entry['line'] != line
                    or entry['page'] != page or entry['classification'] != diagnostic['classification']):
                raise ValueError
            support_sha = entry['support_sha256']
            if not re.fullmatch('[0-9a-f]{64}', support_sha):
                raise ValueError
            support_path = folder / (support_sha + '.json')
            if support_path.is_symlink() or support_path.stat().st_size > 256 * 1024:
                raise ValueError
            raw = support_path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != support_sha:
                raise ValueError
            support = json.loads(raw)
            if diagnostic.get('metadata_shape_proven'):
                if support != diagnostic or entry['disposition'] != 'HEADER_SHAPE_VERIFIED':
                    raise ValueError
            else:
                layout = support['layout']
                if (layout['source_sha256'] != source['sha256'] or layout['page'] != page
                        or layout['warning'] != diagnostic or layout['visual_verified'] is not True
                        or layout['status'] != 'RESOLVED_NON_TRANSACTION'
                        or entry['disposition'] != 'RESOLVED_NON_TRANSACTION' or not support['crops']):
                    raise ValueError
                for sha in support['crops']:
                    if not re.fullmatch('[0-9a-f]{64}', sha):
                        raise ValueError
                    crop = folder / (sha + '.png')
                    if crop.is_symlink() or hashlib.sha256(crop.read_bytes()).hexdigest() != sha:
                        raise ValueError
            entries.append({k: entry[k] for k in ('warning_sha256', 'classification', 'page', 'line',
                                                  'line_sha256', 'support_sha256', 'disposition')})
        return {'status': 'verified_diagnostic', 'binding': digest([packet, source['revision']]) if rebound else digest(packet),
                'entries': entries, 'source_revision': source['revision'],
                'rebound_from_revision': packet['revision'] if rebound else None,
                'source_acceptance_allowed': False, 'financial_completeness_proven': False}
    except (ValueError, KeyError, TypeError, OSError, IndexError):
        return {'status': 'unavailable', 'binding': None, 'entries': [], 'source_acceptance_allowed': False}

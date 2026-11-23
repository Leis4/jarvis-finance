"""Protected local statement preview, explicit acceptance and source-scoped reports."""
from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date
from typing import Annotated, Any

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from jarvis.finance.review_store import ReviewStore
from jarvis.finance.statement_product import StatementProduct


class SourceDecision(BaseModel):
    model_config = ConfigDict(extra='forbid')
    actor: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=0, strict=True)
    accept: bool
    confirmed: bool = False
    acknowledge_parser_warnings: bool = False


class StatementPeriod(BaseModel):
    model_config = ConfigDict(extra='forbid')
    start: date
    end: date
    currency: str = Field(default='RUB', pattern='^[A-Z]{3}$')


class SourceGroupDecision(BaseModel):
    model_config = ConfigDict(extra='forbid')
    actor: str = Field(min_length=1, max_length=100)
    binding: str = Field(pattern='^[0-9a-f]{64}$')
    confirmed: bool = Field(default=False, strict=True)
    acknowledge_parser_warnings: bool = Field(default=False, strict=True)


class CategoryGroupDecision(BaseModel):
    model_config = ConfigDict(extra='forbid')
    group_hash: str = Field(pattern='^[0-9a-f]{64}$')
    actor: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=500)
    confirmed: bool = Field(default=False, strict=True)
    action: str = Field(pattern='^(confirm|reject)$')
    category: str | None = Field(default=None, max_length=100)


class ContextReviewBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    selection: dict[str, Any]


def install_statement_routes(app: FastAPI, store: ReviewStore, authorized: Callable[[Request], None]) -> None:
    """Install without changing review state or opening a financial database."""
    product = StatementProduct(store)

    def refusal() -> HTTPException:
        return HTTPException(409, 'Источник/период не подтверждён или изменился. Обновите preview; проверьте mapping, баланс и оригинал.')

    @app.get('/api/statements/annotation-export', dependencies=[Depends(authorized)])
    def annotation_export(private: bool = False) -> dict[str, Any]:
        from jarvis.finance.classification.annotation import AnnotationExchange
        try:
            return AnnotationExchange(store).export(private=private)
        except (ValueError, KeyError, OSError):
            raise refusal() from None

    @app.get('/api/statements/annotation-context', dependencies=[Depends(authorized)])
    def annotation_context(private: bool = False) -> dict[str, Any]:
        from jarvis.finance.classification.annotation_context import AnnotationContext
        try:
            return AnnotationContext(store).preview(private=private)
        except (ValueError, KeyError, OSError):
            raise refusal() from None

    @app.post('/api/statements/annotation-context-export', dependencies=[Depends(authorized)])
    def annotation_context_export(body: ContextReviewBody) -> dict[str, Any]:
        from jarvis.finance.classification.annotation_context import (
            AnnotationContext,
            ContextExportRequest,
        )
        try:
            return AnnotationContext(store).export_reviewed(ContextExportRequest.model_validate(body.selection))
        except (ValueError, KeyError, OSError):
            raise refusal() from None

    @app.get('/api/statements/annotation-proposals', dependencies=[Depends(authorized)])
    def annotation_proposals(offset: int = 0) -> dict[str, Any]:
        from jarvis.finance.classification.annotation import AnnotationExchange
        try:
            return AnnotationExchange(store).proposals(offset=offset)
        except (ValueError, KeyError, OSError):
            raise refusal() from None

    @app.post('/api/statements/annotation-import', dependencies=[Depends(authorized)])
    async def annotation_import(file: UploadFile, actor: Annotated[str, Form()],
                                confirmed: Annotated[bool, Form()] = False,
                                context_review_file: UploadFile | None = None) -> dict[str, Any]:
        from jarvis.finance.classification.annotation import (
            AnnotationExchange,
            decode_import,
        )
        try:
            raw = await file.read(2 * 1024 * 1024 + 1)
            context = decode_import(await context_review_file.read(2 * 1024 * 1024 + 1)) if context_review_file else None
            return AnnotationExchange(store).import_proposals(decode_import(raw), actor=actor, confirmed=confirmed,
                                                               context_review=context)
        except (ValueError, KeyError, OSError):
            raise refusal() from None
        finally:
            await file.close()
            if context_review_file:
                await context_review_file.close()

    @app.get('/api/statements/history', dependencies=[Depends(authorized)])
    def history() -> dict[str, Any]:
        try:
            return product.history()
        except (ValueError, KeyError, OSError):
            raise refusal() from None

    @app.get('/api/statements/category-review', dependencies=[Depends(authorized)])
    def category_review() -> dict[str, Any]:
        try:
            return product.category_review()
        except (ValueError, KeyError, OSError):
            raise refusal() from None

    @app.post('/api/statements/category-group', dependencies=[Depends(authorized)])
    def category_group(body: CategoryGroupDecision) -> dict[str, Any]:
        try:
            return product.decide_category_group(**body.model_dump())
        except (ValueError, KeyError, OSError):
            raise refusal() from None

    @app.get('/api/statements', dependencies=[Depends(authorized)])
    def sources() -> dict[str, Any]:
        try:
            values = product.sources()
            return {'sources': [{k: s[k] for k in ('id', 'sha256', 'format', 'parser_version', 'metadata', 'reconciliation', 'revision', 'accepted', 'parser_warnings')}
                                | {'operation_count': len(s['transactions'])} for s in values],
                    'scope': 'accepted_statements_only', 'production_database_modified': False}
        except (ValueError, KeyError, OSError):
            raise refusal() from None

    @app.get('/api/statements/pending-review', dependencies=[Depends(authorized)])
    def pending_review() -> dict[str, Any]:
        try:
            return product.pending_source_review()
        except (ValueError, KeyError, OSError):
            raise refusal() from None

    @app.get('/api/statements/group-preview', dependencies=[Depends(authorized)])
    def group_preview() -> dict[str, Any]:
        try:
            return product.group_preview()
        except (ValueError, KeyError, OSError):
            raise refusal() from None

    @app.post('/api/statements/group-decision', dependencies=[Depends(authorized)])
    def group_decision(body: SourceGroupDecision) -> dict[str, Any]:
        try:
            return product.accept_group(body.binding, actor=body.actor, confirmed=body.confirmed,
                acknowledge_parser_warnings=body.acknowledge_parser_warnings)
        except (ValueError, KeyError, OSError):
            raise refusal() from None

    @app.post('/api/statements/preview', dependencies=[Depends(authorized)])
    async def preview(file: Annotated[UploadFile, File()], csv_scope: Annotated[str | None, Form()] = None) -> dict[str, Any]:
        try:
            data = await file.read(20 * 1024 * 1024 + 1)
            if csv_scope is not None and len(csv_scope) > 8192:
                raise ValueError('CSV scope too large')
            scope = json.loads(csv_scope) if csv_scope is not None else None
            if scope is not None and not isinstance(scope, dict):
                raise ValueError('CSV scope must be an object')
            result = product.prepare(data, file.filename or '', csv_scope=scope)
            return {'id': result['id'], 'revision': result['revision'], 'accepted': result['accepted'],
                    'sha256': result['source']['sha256'], 'metadata': result['source']['metadata'],
                    'operation_count': len(result['source']['transactions']),
                    'reconciliation': result['source']['reconciliation'], 'production_database_modified': False}
        except (ValueError, KeyError, TypeError, OSError, ArithmeticError):
            raise refusal() from None
        finally:
            await file.close()

    @app.post('/api/statements/{key}/decision', dependencies=[Depends(authorized)])
    def decision(key: str, body: SourceDecision) -> dict[str, Any]:
        if not body.confirmed:
            raise HTTPException(409, 'Нужно явное подтверждение владельца источника; это отдельное решение от категории.')
        try:
            return product.decide(key, actor=body.actor, expected_revision=body.expected_revision, accept=body.accept,
                                  acknowledge_parser_warnings=body.acknowledge_parser_warnings)
        except (ValueError, KeyError, OSError):
            raise refusal() from None

    @app.get('/api/statements/{key}/original', dependencies=[Depends(authorized)])
    def original(key: str) -> FileResponse:
        try:
            source = product._source(key)
            return FileResponse(store.workspace / 'statement-originals' / str(source['original']),
                filename='private-statement.' + str(source['format']), headers={'Cache-Control': 'no-store'},
                media_type='application/pdf' if source['format'] == 'pdf' else 'text/csv')
        except (ValueError, KeyError, OSError):
            raise refusal() from None

    @app.get('/api/statements/{key}/provenance', dependencies=[Depends(authorized)])
    def provenance(key: str, offset: int = Query(default=0, ge=0), limit: int = Query(default=50, ge=1, le=100)) -> dict[str, Any]:
        from jarvis.finance.bank.sber_provenance import pdf_provenance
        try:
            source = product._source(key)
            if source['format'] != 'pdf':
                raise ValueError('PDF provenance only')
            result = pdf_provenance(store.workspace / 'statement-originals' / source['original'], account=source['metadata']['bank_account'])
            # Compare against the saved accepted/preview payload; never migrate it.
            from jarvis.finance.sber_intake import digest
            if result['source_sha256'] != source['sha256'] or result['parsed_count'] != len(source['transactions']) or any(
                    row['parsed_transaction_sha256'] != digest(source['transactions'][row['ordinal']]) for row in result['rows']):
                raise ValueError('Saved transaction binding differs')
            rows = result['rows'][offset:offset + limit]
            result['rows'] = [{**row, 'authorization_code': ('••' + row['authorization_code'][-2:]) if row['authorization_code'] else None} for row in rows]
            result.update(offset=offset, limit=limit, next_offset=offset + limit if offset + limit < len(source['transactions']) else None,
                          source_id=key, source_revision=max((event['seq'] for event in store.audit()
                              if event.get('domain') == 'statement_source' and event.get('item_id') == 'statement:' + key), default=0),
                          production_database_modified=False)
            return result
        except (ValueError, KeyError, OSError):
            raise refusal() from None

    @app.post('/api/statements/report', dependencies=[Depends(authorized)])
    def report(body: StatementPeriod) -> dict[str, Any]:
        try:
            return product.report(body.start, body.end, body.currency)
        except (ValueError, KeyError, OSError):
            return {'status': 'unavailable', 'income': None, 'expenses': None, 'bank_movements': None,
                    'available_funds': None, 'production_database_modified': False,
                    'reason': 'Примите проверенные выписки. При повреждении/отзыве старые суммы не используются.'}

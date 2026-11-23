"""Protected local setup for shared Telegram review; no network or polling here."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from jarvis.finance.review_store import ReviewStore
from jarvis.finance.telegram.review_runtime import save_token
from jarvis.finance.telegram.shared_review import SharedReview, digest


class Privacy(BaseModel):
    model_config = ConfigDict(extra='forbid')
    fields: list[str] = Field(max_length=5)
    finance_reports: bool = False


class Pair(Privacy):
    code: str = Field(pattern=r'^[0-9A-Fa-f]{8}$')
    expected_preview: str = Field(pattern=r'^[0-9a-f]{64}$')
    confirmed: bool


def install_telegram_routes(app: FastAPI, store: ReviewStore, authorized: Callable[..., None]) -> None:
    review = SharedReview(store, initialize=False)

    @app.get('/api/telegram/status', dependencies=[Depends(authorized)])
    def status() -> dict[str, Any]:
        return review.status()

    @app.post('/api/telegram/token', dependencies=[Depends(authorized)])
    async def token(request: Request) -> dict[str, Any]:
        # Pydantic's default validation detail echoes invalid input; never echo a token.
        try:
            raw = await request.body()
            if len(raw) > 512:
                raise ValueError
            import json
            body = json.loads(raw)
            if set(body) != {'token'} or not isinstance(body['token'], str):
                raise ValueError
            save_token(store, body['token'])
        except (ValueError, TypeError, KeyError):
            raise HTTPException(400, 'Не удалось сохранить токен; проверьте формат и Windows DPAPI.') from None
        return {'saved': True, 'plaintext_stored': False, 'production_database_modified': False}

    @app.post('/api/telegram/preview', dependencies=[Depends(authorized)])
    def preview(body: Privacy) -> dict[str, Any]:
        result = review.preview(body.fields, finance_reports=body.finance_reports)
        return {**result, 'binding': digest(result)}

    @app.post('/api/telegram/pair', dependencies=[Depends(authorized)])
    def pair(body: Pair) -> dict[str, Any]:
        if not body.confirmed:
            return {'enabled': False, 'reason': 'EXPLICIT_CONFIRMATION_REQUIRED'}
        return review.finish_pair(body.code, body.fields, expected_preview=body.expected_preview, finance_reports=body.finance_reports)

    @app.post('/api/telegram/disable', dependencies=[Depends(authorized)])
    def disable() -> dict[str, Any]:
        return review.disable()

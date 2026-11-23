"""Explicit review-only polling; never instantiate legacy finance bot or scheduler."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from jarvis.finance.review_store import ReviewStore, encoded
from jarvis.finance.telegram.bot import TelegramAPI
from jarvis.finance.telegram.shared_review import ReviewRefusal, SharedReview
from jarvis.finance.windows_session_store import save_state, unprotect_state


def save_token(store: ReviewStore, token: str) -> None:
    """Store only current-user DPAPI ciphertext, bound to this workspace and purpose."""
    if not re.fullmatch(r'[0-9]{5,20}:[A-Za-z0-9_-]{20,100}', token):
        raise ReviewRefusal('INVALID_TOKEN_FORMAT')
    # Revocation first: a failed token replacement must never retain an old permission.
    SharedReview(store).disable()
    save_state(store.workspace / 'telegram.dpapi', encoded({
        'purpose': 'telegram-review-v1', 'workspace': str(store.workspace), 'token': token}).encode())


def read_token(store: ReviewStore) -> str:
    path = store.workspace / 'telegram.dpapi'
    if path.is_symlink() or not path.is_file():
        raise ReviewRefusal('TOKEN_UNAVAILABLE')
    try:
        value = json.loads(unprotect_state(path.read_bytes()))
        if value['purpose'] != 'telegram-review-v1' or value['workspace'] != str(store.workspace):
            raise ValueError
        return str(value['token'])
    except Exception:  # noqa: BLE001 - secret-free diagnostic
        raise ReviewRefusal('TOKEN_UNAVAILABLE') from None


class ReviewTransport:
    """Reuse the existing Bot API transport with closed methods and fixed errors."""
    synthetic = False

    def __init__(self, token: str):
        self.api = TelegramAPI(token)
        self.identity = hashlib.sha256(token.encode()).hexdigest()

    def call(self, method: str, payload: dict[str, Any]) -> Any:
        if method not in {'getWebhookInfo', 'getUpdates', 'sendMessage', 'sendPhoto', 'answerCallbackQuery'}:
            raise ReviewRefusal('TRANSPORT_METHOD_REFUSED')
        try:
            wire = dict(payload)
            if 'reply_markup' in wire:
                wire['reply_markup'] = encoded(wire['reply_markup'])
            if method == 'sendPhoto':
                image = base64.b64decode(wire.pop('photo_base64'), validate=True)
                if not image.startswith(b'\x89PNG\r\n\x1a\n') or len(image) > 2_000_000:
                    raise ValueError
                response = self.api.request_photo(wire, image)
            else:
                response = self.api.request(method, wire)
            if not response.get('ok'):
                raise ValueError
            return response['result']
        except Exception:  # noqa: BLE001 - exceptions may contain Bot API token URL
            raise ReviewRefusal('TELEGRAM_TRANSPORT_UNAVAILABLE') from None


@contextmanager
def consumer_lock(token: str) -> Iterator[None]:
    """One local consumer per token; OS releases the lock even after process crash."""
    directory = Path(os.environ.get('LOCALAPPDATA', tempfile.gettempdir())) / 'Jarvis' / 'telegram-locks'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (hashlib.sha256(token.encode()).hexdigest() + '.lock')
    with path.open('a+b') as stream:
        stream.seek(0)
        if os.fstat(stream.fileno()).st_size == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise ReviewRefusal('CONSUMER_ALREADY_RUNNING') from None
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import importlib
            fcntl = importlib.import_module("fcntl")
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise ReviewRefusal('CONSUMER_ALREADY_RUNNING') from None
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)


def poll_once(review: SharedReview, transport: Any) -> int:
    """Advance only terminal updates; an interrupted update blocks automatic replay."""
    with review.store.connect() as conn:
        if conn.execute("SELECT 1 FROM tg_updates WHERE channel=? AND state='started'", (transport.identity,)).fetchone():
            raise ReviewRefusal('INTERRUPTED_UPDATE_REQUIRES_REVIEW')
        offset = conn.execute('SELECT COALESCE(MAX(id)+1,0) FROM tg_updates WHERE channel=?', (transport.identity,)).fetchone()[0]
    updates = transport.call('getUpdates', {'offset': offset, 'timeout': 25,
        'allowed_updates': encoded(['message', 'callback_query'])})
    if not isinstance(updates, list):
        raise ReviewRefusal('INVALID_UPDATES')
    for update in sorted(updates, key=lambda u: u['update_id']):
        review.handle(update, transport)
    return len(updates)


def main() -> int:
    parser = argparse.ArgumentParser(description='Отдельная личная проверка категорий; сайт polling не запускает.')
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--owner-start-polling', action='store_true')
    args = parser.parse_args()
    if not args.owner_start_polling:
        parser.error('Требуется отдельное действие владельца --owner-start-polling')
    try:
        store = ReviewStore(args.workspace)
        review = SharedReview(store)
        token = read_token(store)
        with consumer_lock(token):
            transport = ReviewTransport(token)
            if transport.call('getWebhookInfo', {}).get('url'):
                raise ReviewRefusal('WEBHOOK_CONFIGURED')
            while True:
                poll_once(review, transport)
    except KeyboardInterrupt:
        return 0
    except Exception:  # noqa: BLE001 - never expose private transport traceback
        print('Telegram review остановлен. Проверьте локальные настройки, webhook и журнал; автоматического повтора нет.')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())

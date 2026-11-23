import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jarvis.finance import review_ui
from jarvis.finance.review_store import ReviewStore


def fixture_store(path: Path) -> ReviewStore:
    store = ReviewStore(path)
    store.initialize({"apply_allowed": False, "items": [], "groups": []})
    return store


def client(store: ReviewStore, **kwargs: object) -> TestClient:
    return TestClient(review_ui.create_review_app(store, "x" * 40, **kwargs),
                      base_url="http://127.0.0.1", client=("127.0.0.1", 1234))


def test_private_responses_and_errors_never_cached(tmp_path: Path) -> None:
    store = fixture_store(tmp_path)
    before = store.path.read_bytes()
    c = client(store)
    for response in [c.get('/'), c.get('/api/queue'),
                     c.get('/api/queue', headers={"X-Review-Token": "x" * 40}),
                     c.get('/api/report', headers={"origin": "https://untrusted.example"}),
                     c.get('/missing')]:
        assert response.headers['cache-control'] == 'no-store'
        assert response.headers['referrer-policy'] == 'no-referrer'
        assert response.headers['x-content-type-options'] == 'nosniff'
    assert store.path.read_bytes() == before and store.audit() == []


def test_browser_policy_requires_fresh_nonce_and_blocks_embedding(tmp_path: Path) -> None:
    c = client(fixture_store(tmp_path))
    first, second = c.get('/'), c.get('/')
    nonce = re.search(r'<script nonce="([^"]+)">', first.text).group(1)
    assert f"script-src 'nonce-{nonce}'" in first.headers['content-security-policy']
    assert f'<style nonce="{nonce}">' in first.text
    assert "frame-ancestors 'none'" in first.headers['content-security-policy']
    assert 'unsafe-inline' not in first.headers['content-security-policy']
    assert first.headers['content-security-policy'] != second.headers['content-security-policy']


def test_rate_limit_recovers_without_decisions_or_silent_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = fixture_store(tmp_path)
    clock = [10.0]
    monkeypatch.setattr(review_ui, 'monotonic', lambda: clock[0])
    c = client(store, max_requests=2, request_window_seconds=60)
    c.headers['X-Review-Token'] = 'x' * 40
    assert c.get('/api/queue').status_code == 200
    assert c.get('/api/queue').status_code == 200
    limited = c.get('/api/queue')
    assert limited.status_code == 429 and limited.headers['retry-after'] == '60'
    assert limited.headers['cache-control'] == 'no-store'
    clock[0] += 60
    assert c.get('/api/queue').status_code == 200 and store.audit() == []

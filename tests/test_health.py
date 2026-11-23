import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jarvis.api.main import app, local_readiness
from jarvis.core.config import settings
from jarvis.finance.db import FinanceDB

client = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000))


def test_health() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "scope": "process_liveness"}


def test_ready_missing_database_is_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "missing.sqlite"))
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json()["status"] == "unavailable"
    assert response.json()["sources"]["sqlite"]["status"] == "unavailable"
    assert not (tmp_path / "missing.sqlite").exists()


def test_ready_reads_saved_evidence_without_database_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = FinanceDB(tmp_path / "fixture.sqlite")
    db.init()
    old = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    db.set_setting("source_health:sber_mcp", {"status": "ok", "checked_at": old,
                                                "last_success_at": old, "coverage_end": "2026-01-01"})
    before = hashlib.sha256(db.path.read_bytes()).hexdigest()
    monkeypatch.setattr(settings, "db_path", str(db.path))
    response = client.get("/ready")
    payload = response.json()
    assert payload["status"] == "insufficient_data"
    assert payload["sources"]["sqlite"]["status"] == "available"
    assert payload["sources"]["sber"]["status"] == "stale"
    assert payload["sources"]["moex"]["status"] == "blocked"
    assert payload["coverage_complete"] is False
    assert payload["database_modified"] is False
    assert hashlib.sha256(db.path.read_bytes()).hexdigest() == before


def test_remote_asgi_client_is_denied() -> None:
    remote = TestClient(app, base_url="http://127.0.0.1", client=("203.0.113.10", 50000))
    response = remote.get("/health")
    assert response.status_code == 403
    assert response.json() == {"status": "blocked", "reason": "local_only"}


def test_external_host_header_is_denied() -> None:
    response = client.get("/ready", headers={"Host": "public.example"})
    assert response.status_code == 403


def test_failed_and_malformed_saved_source_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = FinanceDB(tmp_path / "fixture.sqlite")
    db.init()
    db.set_setting("source_health:sber_mcp", {"status": "error", "error_type": "private-secret-value"})
    db.set_setting("source_health:telegram", "malformed")
    monkeypatch.setattr(settings, "db_path", str(db.path))
    payload = client.get("/ready").json()
    assert payload["sources"]["sber"]["status"] == "unavailable"
    assert payload["sources"]["telegram"]["status"] == "unavailable"
    assert "private-secret-value" not in str(payload)


def test_readiness_helper_rejects_naive_clock(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="timezone"):
        local_readiness(tmp_path / "missing.sqlite", now=datetime(2026, 1, 1, tzinfo=UTC).replace(tzinfo=None))

"""Keep test defaults away from production databases, models and credentials."""

import os
import socket
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

_sandbox: tempfile.TemporaryDirectory[str] | None = None
_original_cwd = Path.cwd()
_original_env: dict[str, str] = {}


@pytest.fixture(autouse=True)
def no_external_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any accidental live integration call in the isolated suite."""
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def blocked(sock: socket.socket, address: tuple[str, int]) -> None:
        if address[0] in {"127.0.0.1", "::1", "localhost"}:
            return original_connect(sock, address)
        raise RuntimeError("Network is disabled in isolated tests")

    def blocked_ex(sock: socket.socket, address: tuple[str, int]) -> int:
        if address[0] in {"127.0.0.1", "::1", "localhost"}:
            return original_connect_ex(sock, address)
        raise RuntimeError("Network is disabled in isolated tests")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked_ex)


def pytest_sessionstart(session: pytest.Session) -> None:
    """Isolate relative paths before importing test modules."""
    global _sandbox
    _sandbox = tempfile.TemporaryDirectory(
        prefix="jarvis-tests-", ignore_cleanup_errors=True,
    )
    os.chdir(_sandbox.name)
    for key in list(os.environ):
        if key.startswith(("JARVIS_", "TELEGRAM_", "SBER_", "CBONDS_")) or key in {
            "FINANCE_AGENT_DB", "FINANCE_AGENT_DB_KEY", "FINANCE_DB_KEY", "MLFLOW_TRACKING_URI",
        }:
            _original_env[key] = os.environ.pop(key)
    os.environ.update({
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "ANONYMIZED_TELEMETRY": "False",
    })


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Restore the working directory after all tests."""
    os.chdir(_original_cwd)
    os.environ.update(_original_env)
    # Some third-party database clients retain open handles until process exit.
    # Leave OS-temp cleanup to the OS when Windows prevents immediate removal.
    if _sandbox is not None:
        _sandbox.cleanup()


@pytest.fixture
def isolated_llm_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Exercise the real reservation path with disposable SQLite and synthetic prices."""
    import json
    from datetime import UTC, datetime, timedelta

    from sqlmodel import SQLModel, create_engine

    from jarvis.core.llm import orchestrator as orch
    from jarvis.core.llm.budget_store import BudgetStore

    path = tmp_path / "fixture-llm.sqlite"
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    BudgetStore(path).initialize()
    monkeypatch.setattr(orch, "get_engine", lambda: engine)
    monkeypatch.setattr(orch.settings, "db_path", str(path))
    monkeypatch.setattr(orch.settings, "daily_budget_usd", 1.0)
    monkeypatch.setattr(orch.settings, "monthly_budget_usd", 10.0)
    monkeypatch.setattr(orch, "_reserve_live_budget", lambda: None)
    now = datetime.now(UTC)
    snapshot = tmp_path / "synthetic-prices.json"
    snapshot.write_text(json.dumps({
        "source": "https://example.test/synthetic-prices", "verified_by": "fixture",
        "currency": "USD", "observed_at": (now - timedelta(hours=1)).isoformat(),
        "expires_at": (now + timedelta(hours=1)).isoformat(),
        "models": {model: {"input_usd_per_million": "0.1", "output_usd_per_million": "0.1",
                           "max_input_tokens": 100000}
                   for model in {orch.settings.llm_model, orch.settings.llm_fallback_model,
                                 "fixture-primary", "fixture-fallback"} if model},
    }), encoding="utf-8")
    monkeypatch.setenv("JARVIS_LLM_PRICING_SNAPSHOT", str(snapshot))
    yield path
    engine.dispose()

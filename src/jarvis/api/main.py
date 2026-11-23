import ipaddress
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from jarvis.core.config import settings
from jarvis.core.logging import setup_logging
from jarvis.finance.readiness import local_readiness


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    setup_logging(settings.log_level)
    yield


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def local_only(request: Request, call_next: Any) -> Any:
    """Reject remote callers even when uvicorn is accidentally bound widely."""
    try:
        host = (request.url.hostname or "").lower()
        host_is_local = host == "localhost" or ipaddress.ip_address(host).is_loopback
        if (not host_is_local or request.client is None or
                not ipaddress.ip_address(request.client.host).is_loopback):
            raise ValueError("non-loopback")
    except ValueError:
        return JSONResponse({"status": "blocked", "reason": "local_only"}, status_code=403)
    return await call_next(request)


@app.get("/health")
def health_check() -> dict[str, str]:
    """Process liveness only; this does not assert source or data readiness."""
    return {"status": "ok", "scope": "process_liveness"}




@app.get("/ready")
def readiness_check() -> dict[str, Any]:
    return local_readiness(Path(settings.db_path))

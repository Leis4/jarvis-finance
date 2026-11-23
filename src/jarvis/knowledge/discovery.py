"""Bounded SearXNG JSON discovery; result text never becomes tool authority."""
from __future__ import annotations

import hashlib
import ipaddress
import json
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx

from jarvis.knowledge.public_sources import MAX_BYTES, checked_url


def search_endpoint(value: str) -> str:
    """Require a public HTTPS search endpoint without embedded credentials or query."""
    host = urlsplit(value).hostname or ""
    checked_url(value, frozenset({host}))
    if host in {"localhost", ""} or "." not in host or unquote(urlsplit(value).path) != urlsplit(value).path:
        raise ValueError("Public search endpoint required")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return value
    raise ValueError("DNS search endpoint required")


async def searxng(client: httpx.AsyncClient, endpoint: str, question: str, domains: frozenset[str]) -> dict[str, Any]:
    """Read one search page; ignore snippets, commands and off-scope URLs."""
    search_endpoint(endpoint)
    async with client.stream("GET", endpoint, params={"q": question, "format": "json", "pageno": "1"},
                             headers={"Accept-Encoding": "identity"}) as response:
        if response.status_code != 200:
            return {"status": "unavailable", "reason": "Search HTTP refused; configure a JSON-enabled SearXNG instance", "http_status": response.status_code}
        if response.headers.get("content-encoding", "identity") != "identity" or response.headers.get("content-type", "").split(";")[0] != "application/json":
            raise ValueError("Search JSON representation required")
        raw = bytearray()
        async for chunk in response.aiter_bytes():
            raw.extend(chunk)
            if len(raw) > MAX_BYTES:
                raise ValueError("Search response too large")
    document = json.loads(raw)
    if not isinstance(document, dict) or not isinstance(document.get("results"), list) or len(document["results"]) > 100:
        raise ValueError("Invalid search results")
    urls: list[str] = []
    rejected = 0
    for item in document["results"]:
        value = item.get("url") if isinstance(item, dict) else None
        try:
            if not isinstance(value, str) or len(value) > 1000:
                raise ValueError("Invalid URL")
            parts = urlsplit(value)
            value = parts._replace(fragment="").geturl()
            checked_url(value, domains)
            host = parts.hostname or ""
            if host == "localhost" or "." not in host or any(part in {".", ".."} for part in unquote(parts.path).split("/")):
                raise ValueError("Invalid public host/path")
            try:
                ipaddress.ip_address(host)
            except ValueError:
                pass
            else:
                raise ValueError("Literal IP refused")
            value = parts._replace(scheme="https", netloc=host, fragment="").geturl()
            if value not in urls:
                urls.append(value)
        except ValueError:
            rejected += 1
    return {"status": "available" if len(urls) >= 2 else "insufficient_data", "urls": urls[:3],
            "provider": "searxng", "search_performed": True, "rejected_urls": rejected,
            "response_sha256": hashlib.sha256(raw).hexdigest(), "ranking": "provider_order; not independently verified"}

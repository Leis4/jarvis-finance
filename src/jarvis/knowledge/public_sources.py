"""Bounded public HTTP evidence; never follows source instructions."""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import re
import socket
import sqlite3
import ssl
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from jarvis.knowledge.errors import KnowledgeUnavailable
from jarvis.knowledge.journal import IndexJournal, digest

PARSER = "public-html-v1"
QUESTION = "Объясни дюрацию облигации и её связь с изменением процентных ставок"
MAX_BYTES = 32_768


def checked_url(url: str, domains: frozenset[str]) -> str:
    """Validate canonical HTTPS authority; DNS/connection checks are separate."""
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.hostname not in domains or parts.port not in {None, 443}
            or parts.username or parts.password or parts.fragment or parts.query or
            any(c.isspace() or ord(c) < 32 for c in url) or "\\" in url):
        raise ValueError("Public source URL refused")
    return url


class PinnedHTTPS(httpx.AsyncBaseTransport):
    """One checked DNS resolution; TLS uses original host over the selected IP."""
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        async with asyncio.timeout(10):
            host = request.url.host
            if request.method != "GET" or request.url.scheme != "https" or request.url.port not in {None, 443}:
                raise ValueError("Read-only HTTPS required")
            infos = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            addresses = {str(entry[4][0]) for entry in infos}
            if not addresses or any(not ipaddress.ip_address(ip).is_global or ipaddress.ip_address(ip).is_multicast
                                    or ipaddress.ip_address(ip).is_reserved for ip in addresses):
                raise ValueError("Non-public DNS answer refused")
            ip = min(addresses)
            reader, writer = await asyncio.open_connection(ip, 443, ssl=ssl.create_default_context(), server_hostname=host)
            try:
                if writer.get_extra_info("peername")[0] != ip:
                    raise ValueError("Connected peer identity mismatch")
                path = request.url.raw_path.decode("ascii")
                writer.write(f"GET {path} HTTP/1.1\r\nHost: {host}\r\nAccept-Encoding: identity\r\nConnection: close\r\nUser-Agent: Jarvis-Public-Research/1\r\n\r\n".encode("ascii"))
                await writer.drain()
                raw_headers = await reader.readuntil(b"\r\n\r\n")
                if len(raw_headers) > 16_384:
                    raise ValueError("Oversized headers")
                lines = raw_headers.split(b"\r\n")
                match = re.fullmatch(rb"HTTP/1\.[01] ([0-9]{3})[^\r\n]*", lines[0])
                if match is None:
                    raise ValueError("Unexpected HTTP status")
                pairs = [line.split(b":", 1) for line in lines[1:-2]]
                if any(len(pair) != 2 for pair in pairs):
                    raise ValueError("Invalid HTTP headers")
                headers = httpx.Headers([(pair[0].strip(), pair[1].strip()) for pair in pairs])
                if headers.get("content-encoding", "identity") != "identity":
                    raise ValueError("Compressed response refused")
                body = bytearray()
                if headers.get("transfer-encoding") == "chunked":
                    while True:
                        size_line = await reader.readuntil(b"\r\n")
                        if len(size_line) > 100 or not re.fullmatch(rb"[0-9a-fA-F]+\r\n", size_line):
                            raise ValueError("Invalid chunk framing")
                        size = int(size_line.strip(), 16)
                        if not size:
                            if await reader.readexactly(2) != b"\r\n":
                                raise ValueError("Unsupported trailers or partial response")
                            break
                        if len(body) + size > MAX_BYTES:
                            raise ValueError("Oversized source")
                        body.extend(await reader.readexactly(size))
                        if await reader.readexactly(2) != b"\r\n":
                            raise ValueError("Invalid chunk framing")
                elif "transfer-encoding" in headers:
                    raise ValueError("Unsupported transfer encoding")
                else:
                    while chunk := await reader.read(min(4096, MAX_BYTES + 1 - len(body))):
                        body.extend(chunk)
                        if len(body) > MAX_BYTES:
                            raise ValueError("Oversized source")
                    if "content-length" in headers and int(headers["content-length"]) != len(body):
                        raise ValueError("Partial response")
                return httpx.Response(int(match[1]), headers=headers, content=bytes(body), request=request)
            finally:
                writer.close()
                await writer.wait_closed()


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hidden = 0
        self.parts: list[str] = []
        self.published_at: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "meta" and (attributes.get("property") or attributes.get("name")) in {"article:published_time", "datePublished"}:
            candidate = attributes.get("content")
            if candidate:
                try:
                    published = datetime.fromisoformat(candidate)
                    if published.tzinfo is not None:
                        self.published_at = published.isoformat()
                except ValueError:
                    pass
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag in {"p", "div", "br", "h1", "h2"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


class PublicSources:
    """Use KnowledgeStore's lifecycle journal without loading embeddings."""
    def __init__(self, journal: IndexJournal, urls: tuple[str, ...], domains: frozenset[str], *,
                 transport: httpx.AsyncBaseTransport | None = None, question: str = QUESTION,
                 search_endpoint: str | None = None, discovery_scope: str = "") -> None:
        if (not (2 <= len(urls) <= 3 or (search_endpoint and not urls))) or len(set(urls)) != len(urls):
            raise ValueError("Two or three explicit sources required")
        if transport is not None and not isinstance(transport, httpx.MockTransport):
            raise ValueError("Only pinned production or explicit mock transport allowed")
        self.urls = tuple(checked_url(url, domains) for url in urls)
        self.domains, self.journal = domains, journal
        self.transport = transport or PinnedHTTPS()
        self.synthetic = isinstance(transport, httpx.MockTransport)
        self.enabled = True
        self.question = question
        if search_endpoint is not None:
            from jarvis.knowledge.discovery import search_endpoint as validate_endpoint
            validate_endpoint(search_endpoint)
        self.search_endpoint = search_endpoint
        self.discovery_scope = discovery_scope

    def candidates(self) -> dict[str, Any]:
        result = {"status": "available", "question": self.question, "urls": list(self.urls), "search_performed": False,
                  "mode": "synthetic" if self.synthetic else "public_http", "paid_calls": 0, "cost_rub": "0"}
        if self.search_endpoint:
            result["search_endpoint"] = self.search_endpoint
            result["discovery_scope"] = self.discovery_scope
        return result

    async def discover(self, guard: Callable[[], bool], before_request: Callable[[str], None]) -> dict[str, Any]:
        """Reuse fresh journal evidence; reserve intent before one bounded search request."""
        from jarvis.knowledge.discovery import searxng
        if not self.search_endpoint or not self.enabled or not guard():
            raise PermissionError("Public search permission unavailable")
        identity = "discovery:" + digest([self.search_endpoint, self.question, sorted(self.domains), self.synthetic, self.discovery_scope])
        with self.journal.locked() as conn:
            old = self.journal.states(conn).get(identity)
            if old:
                age = datetime.now(UTC) - datetime.fromisoformat(old["at"])
                if old["state"] != "successful":
                    return {"status": "unavailable", "reason": "Search outcome unresolved; no automatic replay"}
                if not timedelta(0) <= age < timedelta(hours=1):
                    return {"status": "stale", "reason": "Search cache expired; explicit refresh required"}
                return {**old["descriptor"], "cached": True}
            self.journal.append(conn, identity, "public_discovery", "intent", {"question_sha256": digest(self.question)})
        if self.synthetic != isinstance(self.transport, httpx.MockTransport):
            raise PermissionError("Search transport changed")
        before_request(self.search_endpoint)
        async with httpx.AsyncClient(transport=self.transport, trust_env=False, timeout=10, follow_redirects=False) as client:
            result = await searxng(client, self.search_endpoint, self.question, self.domains)
        if not self.enabled or not guard():
            raise PermissionError("Search permission revoked")
        result.update(discovery_observed_at=datetime.now(UTC).isoformat(), synthetic=self.synthetic, cost_rub="0", paid_calls=0,
                      cached=False, cache_id=identity, discovery_binding=digest([self.search_endpoint, self.question, sorted(self.domains)]))
        with self.journal.locked() as conn:
            self.journal.append(conn, identity, "public_discovery", "successful" if result["status"] == "available" else "failed", result)
        return result

    async def read(self, guard: Callable[[], bool] = lambda: True,
                   before_request: Callable[[str], None] = lambda url: None,
                   discovery: dict[str, Any] | None = None) -> dict[str, Any]:
        urls = self.urls
        if self.search_endpoint:
            if discovery is None or discovery.get("discovery_binding") != digest([self.search_endpoint, self.question, sorted(self.domains)]):
                raise PermissionError("Bound discovered evidence required")
            with self.journal.locked() as conn:
                state = self.journal.states(conn).get(str(discovery.get("cache_id", "")))
                if state is None or state["state"] != "successful" or any(
                        discovery.get(k) != v for k, v in state["descriptor"].items() if k != "cached"):
                    raise PermissionError("Discovery evidence changed")
                if not timedelta(0) <= datetime.now(UTC) - datetime.fromisoformat(state["at"]) < timedelta(hours=1):
                    raise PermissionError("Discovery evidence stale")
            urls = tuple(discovery["urls"])
        sources = []
        async with httpx.AsyncClient(transport=self.transport, trust_env=False, timeout=10, follow_redirects=False) as client:
            for original in urls:
                if self.search_endpoint:
                    if not self.enabled or not guard():
                        raise PermissionError("Public session revoked")
                    with self.journal.locked() as conn:
                        states = self.journal.states(conn)
                        prior = sorted((s for s in states.values() if s["operation"] == "public_read"
                            and s["state"] == "successful" and s["descriptor"].get("requested_url") == original
                            and s["descriptor"].get("synthetic") == self.synthetic), key=lambda s: s["seq"], reverse=True)
                    if prior:
                        cached = {"doc_id": prior[0]["doc_id"], **prior[0]["descriptor"]}
                        if self.current([cached]):
                            sources.append(cached)
                            continue
                url = original
                for hop in range(4):
                    if not self.enabled or not guard():
                        raise PermissionError("Public session revoked")
                    checked_url(url, self.domains)
                    if self.synthetic != isinstance(self.transport, httpx.MockTransport):
                        raise PermissionError("Transport identity changed")
                    before_request(url)
                    async with client.stream("GET", url, headers={"Accept-Encoding": "identity"}) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            if hop == 3:
                                raise ValueError("Redirect limit")
                            url = checked_url(urljoin(url, response.headers["location"]), self.domains)
                            continue
                        if response.status_code != 200 or response.headers.get("content-encoding", "identity") != "identity":
                            raise ValueError("Public source unavailable")
                        if response.headers.get("content-type", "").split(";")[0] not in {"text/html", "text/plain"}:
                            raise ValueError("Unsupported public source representation")
                        raw = bytearray()
                        async for chunk in response.aiter_bytes():
                            raw.extend(chunk)
                            if len(raw) > MAX_BYTES:
                                raise ValueError("Oversized source")
                    parser = TextExtractor()
                    parser.feed(bytes(raw).decode("utf-8", errors="strict"))
                    text = "\n".join(" ".join(line.split()) for line in "".join(parser.parts).splitlines() if line.strip())
                    if not text:
                        raise ValueError("No readable public evidence")
                    if len(text.encode()) > 8_000:
                        raise ValueError("Extracted evidence exceeds report boundary")
                    observed = datetime.now(UTC).isoformat()
                    descriptor = {"url": url, "requested_url": original, "title": text.splitlines()[0][:160],
                        "observed_at": observed, "published_at": parser.published_at,
                        "publication_status": "self_reported_unverified" if parser.published_at else "unknown",
                        "source_sha256": hashlib.sha256(raw).hexdigest(), "parser_version": PARSER,
                        "text": text, "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                        "extraction_scope": "HTML text nodes excluding script/style; CSS visibility not evaluated", "synthetic": self.synthetic}
                    ident = "public:" + digest(descriptor)
                    with self.journal.locked() as conn:
                        self.journal.append(conn, ident, "public_read", "successful", descriptor)
                    sources.append({"doc_id": ident, **descriptor})
                    break
        return {"status": "available", "sources": sources, "paid_calls": 0, "cost_rub": "0"}

    def current(self, sources: list[dict[str, Any]]) -> bool:
        try:
            with self.journal.locked() as conn:
                states = self.journal.states(conn)
        except (KnowledgeUnavailable, ValueError, OSError, sqlite3.Error, KeyError, TypeError):
            return False
        latest = {state["descriptor"]["requested_url"]: ident for ident, state in sorted(states.items(), key=lambda item: item[1]["seq"])
                  if state["operation"] in {"public_read", "public_revoke"}}
        return self.enabled and bool(sources) and all(
            latest.get(source["requested_url"]) == source["doc_id"]
            and
            states.get(source["doc_id"], {}).get("state") == "successful"
            and states[source["doc_id"]]["descriptor"] == {k: v for k, v in source.items() if k != "doc_id"}
            and datetime.now(UTC) - datetime.fromisoformat(source["observed_at"]) < timedelta(days=1)
            for source in sources)

    def revoke(self, doc_id: str) -> None:
        with self.journal.locked() as conn:
            state = self.journal.states(conn).get(doc_id)
            if state is None or state["operation"] not in {"public_read", "public_revoke"}:
                raise ValueError("Unknown public source")
            self.journal.append(conn, doc_id, "public_revoke", "revoked", state["descriptor"])

    def explain(self, sources: list[dict[str, Any]]) -> dict[str, Any]:
        if not self.current(sources):
            return {"status": "insufficient_data", "reason": "Source revoked, changed or stale"}
        citations = []
        for source in sources:
            for line in source["text"].splitlines():
                lower = line.lower()
                relevant = (("дюрац" in lower or "duration" in lower) and ("став" in lower or "interest" in lower)) if self.question == QUESTION else len({word for word in re.findall(r"[\w]+", self.question.lower()) if len(word) >= 5 and word in lower}) >= 2
                if relevant and len(line) <= 500:
                    citations.append({"doc_id": source["doc_id"], "url": source["url"], "quote": line[:500],
                                      "source_sha256": source["source_sha256"]})
        sufficient = len({c["source_sha256"] for c in citations}) >= 2
        return {"status": "evidence_only" if sufficient else "insufficient_data", "question": self.question,
                "citations": citations[:6], "answer_confirmed": False, "synthesis": "extractive_only",
                "limitations": ["Цитаты не являются инвестиционной рекомендацией.",
                    "Дата публикации неизвестна; противоречия не разрешены автоматически.",
                    "Модельная интерпретация и платный поиск не выполнялись."],
                "contradictions": "unresolved; compare the cited excerpts", "paid_calls": 0, "cost_rub": "0"}

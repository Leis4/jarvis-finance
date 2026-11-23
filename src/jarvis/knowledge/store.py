"""Evidence-bound document retrieval with explicit backend failures."""
from __future__ import annotations

import hashlib
import math
import struct
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import chromadb

from jarvis.knowledge.errors import KnowledgeStale, KnowledgeUnavailable
from jarvis.knowledge.journal import IndexJournal, digest
from jarvis.knowledge.loader import load_pdf_evidence
from jarvis.rag.embedder import Embedder


class KnowledgeStore:
    def __init__(self, persist_dir: Path = Path("data/knowledge/chroma")) -> None:
        self.persist_dir = persist_dir
        self.embedder = Embedder()
        try:
            self.persist_dir.mkdir(parents=True, exist_ok=True)
            self.client = chromadb.PersistentClient(path=str(self.persist_dir))
            self.collection = self.client.get_or_create_collection(name="knowledge", metadata={
                "embedding_model": self.embedder.model_name, "collection_identity": "world_finance_v2",
            })
            self._identity()
            self.journal = IndexJournal(self.persist_dir / "lineage.sqlite")
        except Exception:  # noqa: BLE001 - backend boundary
            raise KnowledgeUnavailable("Knowledge collection unavailable or incompatible") from None

    def _identity(self) -> None:
        metadata = self.collection.metadata or {}
        if (not isinstance(metadata, dict) or metadata.get("embedding_model") != self.embedder.model_name or
                metadata.get("collection_identity") != "world_finance_v2"):
            raise KnowledgeUnavailable("Knowledge embedding/collection identity mismatch")

    def add_chunks(self, doc_id: str, chunks: list[str], metadata: dict[str, Any] | None = None) -> int:
        with self.journal.locked() as conn:
            if doc_id in self.journal.states(conn):
                raise KnowledgeUnavailable("Verified identity cannot use generic ingestion")
            return self._add_chunks(doc_id, chunks, metadata)

    def _add_chunks(self, doc_id: str, chunks: list[str], metadata: dict[str, Any] | None = None) -> int:
        """Index source-bound chunks; page citations need a separate proven lineage."""
        self._identity()
        meta = dict(metadata or {})
        source_hash = meta.get("source_sha256", "")
        if (not doc_id or not isinstance(source_hash, str) or len(source_hash) != 64 or
                any(c not in "0123456789abcdef" for c in source_hash) or
                not isinstance(meta.get("source_uri"), str) or not meta["source_uri"].strip() or
                not isinstance(meta.get("parser_version"), str) or not meta["parser_version"].strip() or
                any(key in meta for key in ("page", "page_number", "pages", "generation", "lineage_sha256"))):
            raise ValueError("Source hash/URI required; page citations need parser lineage")
        if not chunks or any(not isinstance(chunk, str) or not chunk.strip() for chunk in chunks):
            raise ValueError("Nonempty document chunks required")
        try:
            observed = datetime.fromisoformat(meta["observed_at"])
            if observed.tzinfo is None or observed > datetime.now(UTC):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise ValueError("Aware source observation timestamp required") from None
        try:
            existing = self.collection.get(where={"doc_id": doc_id}, include=["metadatas"])
            existing_meta = existing.get("metadatas") or []
            expected_hashes = {i: hashlib.sha256(chunk.encode()).hexdigest() for i, chunk in enumerate(chunks)}
            if existing_meta and (len(existing_meta) != len(chunks) or
                                 any(m.get("source_sha256") != source_hash or
                                     m.get("source_uri") != meta["source_uri"] or
                                     m.get("observed_at") != meta["observed_at"] or
                                     m.get("parser_version") != meta["parser_version"] or
                                     type(m.get("chunk_index")) is not int or
                                     m.get("chunk_sha256") != expected_hashes.get(cast(int, m.get("chunk_index")))
                                     for m in existing_meta)):
                raise ValueError("Revoke document before replacing source/chunk layout")
            embeddings: Any = self.embedder.encode(chunks)
            ids = [f"{doc_id}_chunk_{i}" for i in range(len(chunks))]
            metas: Any = [{**meta, "doc_id": doc_id, "chunk_index": i,
                           "chunk_sha256": hashlib.sha256(chunk.encode()).hexdigest(),
                           "embedding_model": self.embedder.model_name, "citation_scope": "document"}
                          for i, chunk in enumerate(chunks)]
            self.collection.upsert(ids=ids, embeddings=embeddings, documents=chunks, metadatas=metas)
        except Exception:  # noqa: BLE001 - embedding/backend boundary
            raise KnowledgeUnavailable("Knowledge ingestion unavailable; source retained for rebuild") from None
        return len(chunks)

    def revoke_document(self, doc_id: str) -> None:
        """Remove derived chunks by source identity without touching the original file."""
        self._identity()
        if not doc_id:
            raise ValueError("Document identity required")
        try:
            with self.journal.locked() as conn:
                state = self.journal.states(conn).get(doc_id)
                if state is not None:
                    descriptor = state["descriptor"]
                    if state["operation"] in {"public_read", "public_revoke"}:
                        self.journal.append(conn, doc_id, "public_revoke", "revoked", descriptor)
                        return
                    if state["state"] == "revoked" and not self._backend_records(doc_id):
                        return
                    self.journal.append(conn, doc_id, "revoke", "intent", descriptor)
                    try:
                        self.collection.delete(where={"doc_id": doc_id})
                        if self._backend_records(doc_id):
                            raise KnowledgeUnavailable("Revocation incomplete")
                    except Exception:
                        self.journal.append(conn, doc_id, "revoke", "uncertain", descriptor)
                        raise
                    self.journal.append(conn, doc_id, "revoke", "revoked", descriptor)
                    return
                self.collection.delete(where={"doc_id": doc_id})
        except Exception:  # noqa: BLE001 - backend boundary
            raise KnowledgeUnavailable("Document revocation unavailable") from None

    def _backend_records(self, doc_id: str) -> dict[str, dict[str, Any]]:
        response: Any = self.collection.get(where={"doc_id": doc_id}, include=["documents", "metadatas", "embeddings"])
        ids, docs, metas = response["ids"], response["documents"], response["metadatas"]
        vectors = response["embeddings"]
        if len(ids) != len(docs) or len(ids) != len(metas) or len(ids) != len(vectors) or len(set(ids)) != len(ids):
            raise KnowledgeUnavailable("Backend record shape mismatch")
        return {ident: {"document": doc, "metadata": meta, "embedding": self._vector(vector)}
                for ident, doc, meta, vector in zip(ids, docs, metas, vectors, strict=True)}

    @staticmethod
    def _vector(vector: Any) -> list[float]:
        if len(vector) == 0 or any(isinstance(v, bool) or not math.isfinite(float(v)) for v in vector):
            raise KnowledgeUnavailable("Embedding vector unavailable")
        return [struct.unpack("f", struct.pack("f", float(v)))[0] for v in vector]

    def _page_space(self) -> None:
        config = self.collection.configuration
        if not isinstance(config, dict) or (config.get("hnsw") or config.get("spann") or {}).get("space") != "l2":
            raise KnowledgeUnavailable("Verified citation distance requires L2 collection")

    @staticmethod
    def verify_pdf_bytes(raw: bytes, artifact: dict[str, Any]) -> None:
        """Verify inline originals with the same page parser, without vector/model calls."""
        from jarvis.knowledge.pdf_process import process_pdf
        actual = process_pdf(raw, chunk_size=artifact["chunk_size"], overlap=artifact["overlap"])
        if artifact != actual:
            raise KnowledgeUnavailable("PDF artifact does not match original/parser/chunk lineage")

    @staticmethod
    def _verify_artifact(source: Path, artifact: dict[str, Any]) -> None:
        actual = load_pdf_evidence(source, chunk_size=artifact["chunk_size"], overlap=artifact["overlap"])
        if artifact != actual:
            raise KnowledgeUnavailable("PDF artifact does not match original/parser/chunk lineage")

    def index_pdf(self, doc_id: str, source: Path, artifact: dict[str, Any], *, observed_at: datetime) -> int:
        """Verify original page lineage, persist intent, then reconcile backend outcome."""
        self._identity()
        if not doc_id or observed_at.tzinfo is None or observed_at > datetime.now(UTC):
            raise ValueError("Document identity and aware nonfuture observation required")
        try:
            self._verify_artifact(source, artifact)
            self._page_space()
            evidence = {"artifact": artifact, "embedding_model": self.embedder.model_name,
                        "collection_identity": "world_finance_v2", "observed_at": observed_at.isoformat()}
            generation = digest(evidence)
            records = {f"{doc_id}:{generation}:{chunk['chunk_index']}": {
                "document": chunk["text"], "metadata": {"doc_id": doc_id, "generation": generation,
                    "source_sha256": artifact["source_sha256"], "parser_version": artifact["parser_version"],
                    "lineage_sha256": artifact["lineage_sha256"], "chunk_index": chunk["chunk_index"],
                    "chunk_sha256": chunk["chunk_sha256"], "page": chunk["page"], "citation_scope": "page",
                    "embedding_model": self.embedder.model_name, "observed_at": observed_at.isoformat()}}
                for chunk in artifact["chunks"]}
            with self.journal.locked() as conn:
                previous = self.journal.states(conn).get(doc_id)
                if previous and previous["state"] not in {"successful", "revoked", "failed"}:
                    raise KnowledgeUnavailable("Recovery required before index retry")
                vectors: Any = self.embedder.encode([r["document"] for r in records.values()])
                if len(vectors) != len(records):
                    raise KnowledgeUnavailable("Embedding count mismatch")
                for record, vector in zip(records.values(), vectors, strict=True):
                    record["embedding"] = self._vector(vector)
                if len({len(r["embedding"]) for r in records.values()}) != 1:
                    raise KnowledgeUnavailable("Embedding dimensions mismatch")
                descriptor = {**evidence, "source_path": str(source.resolve()), "generation": generation, "records": records}
                if previous and previous["state"] == "successful":
                    if previous["descriptor"] != descriptor or self._backend_records(doc_id) != records:
                        raise KnowledgeUnavailable("Revoke before changing index generation")
                    return len(records)
                if previous and previous["state"] not in {"revoked", "failed"}:
                    raise KnowledgeUnavailable("Recovery required before index retry")
                if self._backend_records(doc_id):
                    raise KnowledgeUnavailable("Existing unverified records require explicit revocation")
                self.journal.append(conn, doc_id, "index", "intent", descriptor)
                try:
                    embeddings: Any = [r["embedding"] for r in records.values()]
                    self.collection.upsert(ids=list(records), embeddings=embeddings,
                        documents=[r["document"] for r in records.values()],
                        metadatas=[r["metadata"] for r in records.values()])
                    self._verify_artifact(source, artifact)
                    if self._backend_records(doc_id) != records:
                        raise KnowledgeUnavailable("Index backend outcome mismatch")
                except Exception:
                    self.journal.append(conn, doc_id, "index", "uncertain", descriptor)
                    raise
                self.journal.append(conn, doc_id, "index", "successful", descriptor)
            return len(records)
        except Exception:  # noqa: BLE001 - artifact/backend boundary
            raise KnowledgeUnavailable("Verified PDF ingestion unavailable") from None

    def recover_index(self, doc_id: str) -> str:
        """Observe backend after interrupted work; never repeat an unknown mutation."""
        self._identity()
        try:
            with self.journal.locked() as conn:
                prior = self.journal.states(conn)[doc_id]
                descriptor = prior["descriptor"]
                if prior["operation"] in {"public_read", "public_revoke"}:
                    return str(prior["state"])
                actual = self._backend_records(doc_id)
                if prior["operation"] == "revoke":
                    state = "revoked" if not actual else "uncertain"
                else:
                    self._page_space()
                    self._verify_artifact(Path(descriptor["source_path"]), descriptor["artifact"])
                    if descriptor["embedding_model"] != self.embedder.model_name:
                        raise KnowledgeUnavailable("Embedding generation mismatch")
                    state = "successful" if actual == descriptor["records"] else "uncertain" if actual else "failed"
                self.journal.append(conn, doc_id, prior["operation"], state, descriptor)
                return state
        except Exception:  # noqa: BLE001 - recovery boundary
            raise KnowledgeUnavailable("Knowledge recovery unavailable; mutation not replayed") from None

    def query(
        self, query_text: str, n_results: int = 3, *,
        max_observation_age: timedelta | None = None, now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Hold lifecycle writer guard through provenance verification and retrieval."""
        if type(n_results) is not int or n_results <= 0 or not query_text.strip():
            raise ValueError("Query and positive result limit required")
        if now is not None and now.tzinfo is None:
            raise ValueError("Aware retrieval clock required")
        if max_observation_age is not None and max_observation_age <= timedelta(0):
            raise ValueError("Positive observation age policy required")
        try:
            with self.journal.locked() as conn:
                states = {key: state for key, state in self.journal.states(conn).items()
                          if state["operation"] not in {"public_read", "public_revoke"}}
                for state in states.values():
                    if state["state"] not in {"successful", "revoked"}:
                        raise KnowledgeUnavailable("Knowledge lifecycle requires recovery")
                    if state["state"] == "successful":
                        descriptor = state["descriptor"]
                        self._verify_artifact(Path(descriptor["source_path"]), descriptor["artifact"])
                        if (descriptor["embedding_model"] != self.embedder.model_name or
                                self._backend_records(state["doc_id"]) != descriptor["records"]):
                            raise KnowledgeUnavailable("Accepted index generation mismatch")
                if states and all(s["state"] == "revoked" for s in states.values()) and self.collection.count() == 0:
                    raise KnowledgeUnavailable("Verified sources revoked")
                results = self._query(query_text, n_results, max_observation_age=max_observation_age, now=now, states=states)
                for state in states.values():
                    if state["state"] == "successful":
                        self._verify_artifact(Path(state["descriptor"]["source_path"]), state["descriptor"]["artifact"])
                        if self._backend_records(state["doc_id"]) != state["descriptor"]["records"]:
                            raise KnowledgeUnavailable("Index changed during retrieval")
                return results
        except KnowledgeUnavailable:
            raise
        except Exception:  # noqa: BLE001 - journal boundary
            raise KnowledgeUnavailable("Knowledge lifecycle unavailable") from None

    def _query(
        self, query_text: str, n_results: int, *, max_observation_age: timedelta | None,
        now: datetime | None, states: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        self._identity()
        if type(n_results) is not int or n_results <= 0 or not query_text.strip():
            raise ValueError("Query and positive result limit required")
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            raise ValueError("Aware retrieval clock required")
        if max_observation_age is not None and max_observation_age <= timedelta(0):
            raise ValueError("Positive observation age policy required")
        try:
            emb: Any = self.embedder.encode([query_text])[0]
            res: Any = self.collection.query(query_embeddings=[emb], n_results=n_results)
            if any(not isinstance(res.get(key), list) or len(res[key]) != 1
                   for key in ("documents", "distances", "metadatas")):
                raise ValueError
            docs = res["documents"][0]
            dists = res["distances"][0]
            metas = res["metadatas"][0]
            if not isinstance(docs, list) or not isinstance(dists, list) or not isinstance(metas, list):
                raise TypeError
            if len(docs) != len(dists) or len(docs) != len(metas):
                raise ValueError
            results: list[dict[str, Any]] = []
            for doc, dist, meta in zip(docs, dists, metas, strict=True):
                if (not isinstance(doc, str) or not isinstance(dist, (float, int)) or isinstance(dist, bool) or
                        not math.isfinite(dist) or dist < 0 or not isinstance(meta, dict) or
                        meta.get("embedding_model") != self.embedder.model_name or
                        meta.get("chunk_sha256") != hashlib.sha256(doc.encode()).hexdigest() or
                        not meta.get("source_sha256") or not meta.get("parser_version")):
                    raise ValueError
                page_bound = meta.get("citation_scope") == "page"
                if page_bound:
                    self._page_space()
                    state = states.get(str(meta.get("doc_id")))
                    if not state or state["state"] != "successful":
                        raise ValueError
                    ids = res["ids"][0]
                    if len(ids) != len(docs) or len(set(ids)) != len(ids):
                        raise ValueError
                    ident = ids[len(results)]
                    record = state["descriptor"]["records"].get(ident)
                    if not record or record["document"] != doc or record["metadata"] != meta:
                        raise ValueError
                    query_vector = self._vector(emb)
                    distance = sum((a - b) ** 2 for a, b in zip(query_vector, record["embedding"], strict=True))
                    if not math.isclose(dist, distance, rel_tol=1e-4, abs_tol=1e-5):
                        raise ValueError
                elif (not meta.get("source_uri") or any(k in meta for k in ("page", "page_number", "pages", "generation")) or
                        meta.get("citation_scope") != "document" or meta.get("doc_id") in states):
                    raise ValueError
                observed = datetime.fromisoformat(meta["observed_at"])
                if observed.tzinfo is None or observed > current:
                    raise ValueError
                age = current - observed
                if max_observation_age is not None and age > max_observation_age:
                    raise KnowledgeStale("Knowledge observation exceeds requested age policy")
                results.append({"document": doc, "distance": dist, "metadata": meta,
                                **({"citation": {"page": meta["page"], "source_sha256": meta["source_sha256"],
                                    "lineage_sha256": meta["lineage_sha256"], "generation": meta["generation"]}}
                                   if page_bound else {}),
                                "freshness": "observation_fresh" if max_observation_age is not None else "unverified",
                                "observation_age_seconds": age.total_seconds()})
            return results
        except KnowledgeStale:
            raise
        except Exception:  # noqa: BLE001 - backend/model/response boundary
            raise KnowledgeUnavailable("Knowledge retrieval unavailable or evidence invalid") from None

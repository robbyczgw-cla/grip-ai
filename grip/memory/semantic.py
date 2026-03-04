from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests
from loguru import logger

EMBEDDING_MODEL = "nomic-embed-text-v1_5"


def get_embedding(text: str, groq_api_key: str) -> list[float]:
    """Get embedding vector from Groq OpenAI-compatible embeddings endpoint."""
    if not text or not text.strip():
        raise ValueError("text cannot be empty")
    if not groq_api_key:
        raise ValueError("groq_api_key is required")

    resp = requests.post(
        "https://api.groq.com/openai/v1/embeddings",
        headers={
            "Authorization": f"Bearer {groq_api_key}",
            "Content-Type": "application/json",
        },
        json={"model": EMBEDDING_MODEL, "input": text},
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    data = payload.get("data") or []
    if not data:
        raise RuntimeError("Groq embeddings response missing data")
    embedding = data[0].get("embedding")
    if not isinstance(embedding, list) or not embedding:
        raise RuntimeError("Groq embeddings response missing embedding vector")
    return embedding


class SemanticMemory:
    """Persistent semantic memory backed by ChromaDB + Groq embeddings."""

    def __init__(self, groq_api_key: str, persist_path: str | Path | None = None) -> None:
        if not groq_api_key:
            raise ValueError("groq_api_key is required")
        self._groq_api_key = groq_api_key
        self._persist_path = Path(persist_path or (Path.home() / ".grip" / "memory" / "chroma"))
        self._persist_path.mkdir(parents=True, exist_ok=True)

        import chromadb

        self._client = chromadb.PersistentClient(path=str(self._persist_path))
        self._collection = self._client.get_or_create_collection(name="grip_memory")

    @staticmethod
    def _make_id(text: str, metadata: dict[str, Any]) -> str:
        stamp = datetime.now(UTC).isoformat()
        payload = f"{stamp}\n{text}\n{json.dumps(metadata or {}, sort_keys=True, ensure_ascii=False)}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def add(self, text: str, metadata: dict[str, Any]) -> str:
        if not text or not text.strip():
            raise ValueError("text cannot be empty")
        metadata = dict(metadata or {})
        metadata.setdefault("created_at", datetime.now(UTC).isoformat())

        embedding = get_embedding(text, self._groq_api_key)
        doc_id = self._make_id(text, metadata)
        self._collection.add(
            ids=[doc_id],
            documents=[text],
            metadatas=[metadata],
            embeddings=[embedding],
        )
        return doc_id

    def search(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        if not query or not query.strip():
            return []
        k = max(1, min(int(top_k), 20))
        embedding = get_embedding(query, self._groq_api_key)
        results = self._collection.query(
            query_embeddings=[embedding],
            n_results=k,
            include=["documents", "metadatas", "distances"],
        )

        ids = (results.get("ids") or [[]])[0]
        docs = (results.get("documents") or [[]])[0]
        metas = (results.get("metadatas") or [[]])[0]
        dists = (results.get("distances") or [[]])[0]

        out: list[dict[str, Any]] = []
        for i, doc_id in enumerate(ids):
            out.append(
                {
                    "id": doc_id,
                    "text": docs[i] if i < len(docs) else "",
                    "metadata": metas[i] if i < len(metas) else {},
                    "distance": dists[i] if i < len(dists) else None,
                }
            )
        return out

    def count(self) -> int:
        try:
            return int(self._collection.count())
        except Exception as exc:
            logger.debug("SemanticMemory.count failed: {}", exc)
            return 0

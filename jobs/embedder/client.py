from __future__ import annotations

import math
from typing import Any, Literal

import httpx

from vchat.settings import cfg


EmbeddingPriority = Literal["batch", "realtime"]
_BATCH_PRIORITY_HEADER = "X-Dzen-Embedding-Priority"


def _embedding_url(path: str) -> str:
    return f"{cfg.embedding_service_url.rstrip('/')}{path}"


def _parse_embeddings(payload: Any, *, expected_count: int) -> list[list[float]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("dzen_embedder returned an invalid embeddings response")

    rows = payload["data"]
    if len(rows) != expected_count:
        raise ValueError(
            "dzen_embedder returned "
            f"{len(rows)} embeddings for {expected_count} inputs"
        )

    vectors: list[list[float]] = []
    for expected_index, row in enumerate(rows):
        if not isinstance(row, dict) or row.get("index") != expected_index:
            raise ValueError("dzen_embedder returned embeddings out of order")
        embedding = row.get("embedding")
        if not isinstance(embedding, list) or len(embedding) != cfg.vec_dim:
            raise ValueError(
                "dzen_embedder returned an embedding with unexpected dimension "
                f"(expected {cfg.vec_dim})"
            )
        vector = [float(value) for value in embedding]
        if not all(math.isfinite(value) for value in vector):
            raise ValueError("dzen_embedder returned a non-finite embedding value")
        vectors.append(vector)
    return vectors


def embed_texts(
    texts: list[str], *, priority: EmbeddingPriority = "realtime"
) -> list[list[float]]:
    """Embed texts through the machine-local dzen_embedder service."""

    if not texts:
        return []

    headers = {}
    if priority == "batch":
        headers[_BATCH_PRIORITY_HEADER] = "batch"

    response = httpx.post(
        _embedding_url("/v1/embeddings"),
        headers=headers,
        json={
            "model": cfg.embedding_model_id,
            "input": texts,
            "encoding_format": "float",
        },
        timeout=cfg.embedding_service_timeout_seconds,
    )
    response.raise_for_status()
    return _parse_embeddings(response.json(), expected_count=len(texts))


def ready() -> dict[str, Any]:
    """Return the service readiness payload or fail with the original HTTP error."""

    response = httpx.get(
        _embedding_url("/ready"), timeout=cfg.embedding_service_timeout_seconds
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        raise ValueError("dzen_embedder is not ready")
    return payload

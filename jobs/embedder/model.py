import logging
from typing import Any

import torch
from sentence_transformers import SentenceTransformer

from jobs.embedder.tokenizer import (
    load_embedding_tokenizer as load_embedding_tokenizer,
)
from vchat.settings import cfg


def detect_best_device() -> str:
    if torch.cuda.is_available():
        return "cuda"

    mps_backend = getattr(torch.backends, "mps", None)
    if mps_backend and mps_backend.is_available():
        return "mps"

    return "cpu"


def resolve_torch_backend(device: str, *, purpose: str) -> str:
    if device == "cpu":
        return device
    if device == "cuda" and torch.cuda.is_available():
        return device
    mps_backend = getattr(torch.backends, "mps", None)
    if device == "mps" and mps_backend and mps_backend.is_available():
        return device
    raise RuntimeError(f"{purpose} device {device} was requested but is unavailable")


def resolve_embedding_device() -> str:
    if cfg.embedding_device == "auto":
        return detect_best_device()
    return resolve_torch_backend(cfg.embedding_device, purpose="Embedding")


def load_embedding_model(
    *,
    tokenizer_kwargs: dict[str, Any] | None = None,
) -> SentenceTransformer:
    resolved_device = resolve_embedding_device()
    effective_tokenizer_kwargs = dict(tokenizer_kwargs or {})
    if cfg.embedding_max_seq_length > 0:
        effective_tokenizer_kwargs.setdefault("truncation", True)
        effective_tokenizer_kwargs.setdefault(
            "max_length", cfg.embedding_max_seq_length
        )
    logging.info(
        "Loading embedding model models/embedder on %s",
        resolved_device,
    )
    model = SentenceTransformer(
        "models/embedder",
        device=resolved_device,
        tokenizer_kwargs=effective_tokenizer_kwargs or None,
        trust_remote_code=True,
    )
    if cfg.embedding_max_seq_length > 0:
        model.max_seq_length = cfg.embedding_max_seq_length
    return model


def release_torch_cache() -> None:
    mps_backend = getattr(torch.backends, "mps", None)
    if mps_backend and mps_backend.is_available():
        torch.mps.empty_cache()

    if torch.cuda.is_available():
        torch.cuda.empty_cache()

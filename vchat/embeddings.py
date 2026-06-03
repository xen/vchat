import logging
import os
from typing import Any

import torch
from sentence_transformers import SentenceTransformer

from vchat.settings import config


def detect_best_device() -> str:
    if torch.cuda.is_available():
        return "cuda"

    mps_backend = getattr(torch.backends, "mps", None)
    if mps_backend and mps_backend.is_available():
        return "mps"

    return "cpu"


def resolve_embedding_device(preferred: str | None = None) -> str:
    normalized = (preferred or "").strip().lower()
    if not normalized:
        normalized = (os.getenv("EMBEDDING_DEVICE") or "").strip().lower()
    if not normalized:
        normalized = (config.get("embedding_device") or "").strip().lower()

    if normalized in {"auto", ""}:
        return detect_best_device()

    if normalized == "cuda":
        return "cuda" if torch.cuda.is_available() else "cpu"

    if normalized == "mps":
        mps_backend = getattr(torch.backends, "mps", None)
        return "mps" if (mps_backend and mps_backend.is_available()) else "cpu"

    if normalized == "cpu":
        return "cpu"

    return detect_best_device()


def load_embedding_model(
    *,
    device: str | None = None,
    tokenizer_kwargs: dict[str, Any] | None = None,
) -> SentenceTransformer:
    resolved_device = resolve_embedding_device(device)
    max_seq_length = int(config.get("embedding_max_seq_length") or 0)
    effective_tokenizer_kwargs = dict(tokenizer_kwargs or {})
    if max_seq_length > 0:
        effective_tokenizer_kwargs.setdefault("truncation", True)
        effective_tokenizer_kwargs.setdefault("max_length", max_seq_length)
    logging.info(
        "Loading embedding model %s on %s",
        config["embedding_model_dir"],
        resolved_device,
    )
    model = SentenceTransformer(
        config["embedding_model_dir"],
        device=resolved_device,
        tokenizer_kwargs=effective_tokenizer_kwargs or None,
        trust_remote_code=True,
    )
    if max_seq_length > 0:
        model.max_seq_length = max_seq_length
    return model


def release_torch_cache() -> None:
    mps_backend = getattr(torch.backends, "mps", None)
    if mps_backend and mps_backend.is_available():
        torch.mps.empty_cache()

    if torch.cuda.is_available():
        torch.cuda.empty_cache()

from typing import Any
import logging

from sentence_transformers import SentenceTransformer

from vchat.settings import config


def detect_best_device() -> str:
    try:
        import torch
    except Exception:
        return "cpu"

    if torch.cuda.is_available():
        return "cuda"

    mps_backend = getattr(torch.backends, "mps", None)
    if mps_backend and mps_backend.is_available():
        return "mps"

    return "cpu"


def resolve_embedding_device(preferred: str | None = None) -> str:
    normalized = (preferred or "").strip().lower()
    if not normalized:
        normalized = (config.get("embedding_device") or "").strip().lower()

    if normalized in {"auto", ""}:
        return detect_best_device()

    if normalized == "cuda":
        try:
            import torch
        except Exception:
            return "cpu"
        return "cuda" if torch.cuda.is_available() else "cpu"

    if normalized == "mps":
        try:
            import torch
        except Exception:
            return "cpu"
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
    logging.info(
        "Loading embedding model %s on %s",
        config["embedding_model_dir"],
        resolved_device,
    )
    return SentenceTransformer(
        config["embedding_model_dir"],
        device=resolved_device,
        tokenizer_kwargs=tokenizer_kwargs or None,
    )

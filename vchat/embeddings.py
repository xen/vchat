from typing import Any
import logging

from sentence_transformers import SentenceTransformer

from vchat.settings import config


def _ensure_default_rope_init_function() -> None:
    """Backfill missing 'default' rope type for older/newer transformers variants."""
    try:
        from transformers.modeling_rope_utils import ROPE_INIT_FUNCTIONS
    except Exception:
        return

    if "default" in ROPE_INIT_FUNCTIONS:
        return

    fallback_key = None
    for candidate in ("linear", "dynamic", "base", "standard"):
        if candidate in ROPE_INIT_FUNCTIONS:
            fallback_key = candidate
            break

    if fallback_key is None and ROPE_INIT_FUNCTIONS:
        fallback_key = next(iter(ROPE_INIT_FUNCTIONS.keys()))

    if fallback_key is None:
        return

    ROPE_INIT_FUNCTIONS["default"] = ROPE_INIT_FUNCTIONS[fallback_key]
    logging.warning(
        "ROPE_INIT_FUNCTIONS has no 'default'; aliased it to '%s' for model compatibility",
        fallback_key,
    )


def _detect_best_device() -> str:
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
        return _detect_best_device()

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

    # Unknown value -> auto detect instead of failing startup.
    return _detect_best_device()


def load_embedding_model(
    *,
    device: str | None = None,
    tokenizer_kwargs: dict[str, Any] | None = None,
) -> SentenceTransformer:
    _ensure_default_rope_init_function()
    resolved_device = resolve_embedding_device(device)
    return SentenceTransformer(
        config["embedding_model_dir"],
        device=resolved_device,
        tokenizer_kwargs=tokenizer_kwargs or {"padding_side": "left"},
        trust_remote_code=True,
    )

import logging
from typing import Any

from huggingface_hub import snapshot_download
from sentence_transformers import SentenceTransformer

from vchat.settings import config

logger = logging.getLogger(__name__)

DEFAULT_EMBEDDING_MODEL_ID = "Qwen/Qwen3-Embedding-0.6B"


def _config_bool(key: str, default: bool) -> bool:
    value = config.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def resolve_embedding_model_path(model_id: str | None = None) -> str:
    repo_id = model_id or config.get("embedding_model_id") or DEFAULT_EMBEDDING_MODEL_ID
    cache_dir = config.get("huggingface_cache_dir") or None
    local_only = _config_bool("embedding_local_files_only", True)
    allow_remote_fallback = _config_bool("embedding_allow_remote_fallback", False)

    try:
        # Prefer local cache to avoid any network calls during startup.
        return snapshot_download(
            repo_id=repo_id,
            local_files_only=True,
            cache_dir=cache_dir,
        )
    except Exception as exc:
        if local_only and not allow_remote_fallback:
            raise RuntimeError(
                "Embedding model is not available in local HuggingFace cache. "
                "Preload it once or set embedding_allow_remote_fallback=true."
            ) from exc

        logger.warning(
            "Embedding model was not found in local cache; downloading from HuggingFace: %s",
            repo_id,
        )
        return snapshot_download(
            repo_id=repo_id,
            local_files_only=False,
            cache_dir=cache_dir,
        )


def load_embedding_model(
    *,
    device: str = "cpu",
    model_id: str | None = None,
    tokenizer_kwargs: dict[str, Any] | None = None,
) -> SentenceTransformer:
    model_path = resolve_embedding_model_path(model_id=model_id)
    return SentenceTransformer(
        model_path,
        device=device,
        tokenizer_kwargs=tokenizer_kwargs or {"padding_side": "left"},
        trust_remote_code=True,
    )


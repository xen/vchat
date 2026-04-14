from typing import Any

from sentence_transformers import SentenceTransformer

from vchat.settings import config


def load_embedding_model(
    *,
    device: str = "cpu",
    tokenizer_kwargs: dict[str, Any] | None = None,
) -> SentenceTransformer:
    return SentenceTransformer(
        config["embedding_model_dir"],
        device=device,
        tokenizer_kwargs=tokenizer_kwargs or {"padding_side": "left"},
        trust_remote_code=True,
    )

from pathlib import Path
from tokenizers import Tokenizer

from vchat.settings import cfg


class EmbeddingTokenizer:
    def __init__(self, tokenizer: Tokenizer, *, model_max_length: int | None = None):
        self._tokenizer = tokenizer
        self.model_max_length = model_max_length

    def __call__(
        self,
        text: str,
        *,
        add_special_tokens: bool = False,
        truncation: bool = False,
        verbose: bool = True,
    ) -> dict[str, list[int]]:
        encoding = self._tokenizer.encode(
            text or "",
            add_special_tokens=add_special_tokens,
        )
        input_ids = encoding.ids
        if truncation and self.model_max_length and self.model_max_length > 0:
            input_ids = input_ids[: self.model_max_length]
        return {"input_ids": input_ids}

    def decode(
        self,
        ids: list[int],
        *,
        skip_special_tokens: bool = True,
        clean_up_tokenization_spaces: bool = False,
    ) -> str:
        return self._tokenizer.decode(ids, skip_special_tokens=skip_special_tokens)


def load_embedding_tokenizer() -> EmbeddingTokenizer:
    tokenizer_path = Path("models/embedder") / "tokenizer.json"
    return EmbeddingTokenizer(
        Tokenizer.from_file(str(tokenizer_path)),
        model_max_length=(
            cfg.embedding_max_seq_length if cfg.embedding_max_seq_length > 0 else None
        ),
    )

from typing import Any
import logging

from sentence_transformers import SentenceTransformer

from vchat.settings import config


def _patch_transformers_compat() -> None:
    """Patch transformers API changes that break Giga-Embeddings-instruct model loading."""
    try:
        from transformers.modeling_rope_utils import ROPE_INIT_FUNCTIONS
        import transformers.modeling_utils as mu
    except Exception:
        return

    # 1. Inject missing 'default' RoPE — models with rope_scaling=None need standard
    #    unscaled positional encoding; newer transformers removed 'default' from the registry.
    if "default" not in ROPE_INIT_FUNCTIONS:
        def _default_rope(config, device=None, seq_len=None, **kwargs):
            import torch

            base = getattr(config, "rope_theta", 10000.0)
            partial = getattr(config, "partial_rotary_factor", 1.0)
            head_dim = getattr(
                config,
                "head_dim",
                config.hidden_size // config.num_attention_heads,
            )
            dim = int(head_dim * partial)
            inv_freq = 1.0 / (
                base
                ** (torch.arange(0, dim, 2, dtype=torch.int64).float().to(device) / dim)
            )
            return inv_freq, 1.0

        ROPE_INIT_FUNCTIONS["default"] = _default_rope
        logging.warning("ROPE_INIT_FUNCTIONS: injected 'default' (standard unscaled RoPE)")

    # 2. Guard _move_missing_keys_from_meta_to_device against models that don't have
    #    all_tied_weights_keys set (GigarEmbedModel sub-model init ordering issue).
    original_move = getattr(mu.PreTrainedModel, "_move_missing_keys_from_meta_to_device", None)
    if original_move and not getattr(original_move, "_patched_compat", False):
        def _safe_move(self, *args, **kwargs):
            if not hasattr(self, "all_tied_weights_keys"):
                self.all_tied_weights_keys = getattr(self, "_tied_weights_keys", None) or {}
            return original_move(self, *args, **kwargs)

        _safe_move._patched_compat = True
        mu.PreTrainedModel._move_missing_keys_from_meta_to_device = _safe_move
        logging.warning("PreTrainedModel: patched _move_missing_keys_from_meta_to_device for all_tied_weights_keys")


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


def patch_gigar_embed_cpu_forward(st_model: SentenceTransformer) -> None:
    """Patch GigarEmbedModel.forward to run under CPU bfloat16 autocast.

    The model's forward() hard-codes torch.autocast('cuda', bfloat16), which is a
    no-op on CPU.  MLA attention then runs in fp32 and produces all-NaN output.
    Wrapping with autocast('cpu', bfloat16) forces bf16 computation on CPU too.
    """
    try:
        import torch
        inner = st_model._first_module()
        cls = inner.__class__
        if cls.__name__ != "GigarEmbedModel":
            return
        orig = cls.forward
        if getattr(orig, "_patched_cpu_autocast", False):
            return

        def patched_forward(self, *args, **kwargs):
            with torch.autocast("cpu", dtype=torch.bfloat16):
                return orig(self, *args, **kwargs)

        patched_forward._patched_cpu_autocast = True
        cls.forward = patched_forward
        logging.warning(
            "GigarEmbedModel: patched forward() with CPU bfloat16 autocast"
            " (no CUDA — fp32 MLA attention produces NaN)"
        )
    except Exception:
        pass


def load_embedding_model(
    *,
    device: str | None = None,
    tokenizer_kwargs: dict[str, Any] | None = None,
) -> SentenceTransformer:
    _patch_transformers_compat()
    resolved_device = resolve_embedding_device(device)

    # Load weights in bfloat16 on CPU: halves memory vs fp32 and ensures the
    # numerical regime matches what patch_gigar_embed_cpu_forward enforces.
    model_kwargs: dict[str, Any] = {}
    if resolved_device == "cpu":
        try:
            import torch
            model_kwargs["torch_dtype"] = torch.bfloat16
        except Exception:
            pass

    st_model = SentenceTransformer(
        config["embedding_model_dir"],
        device=resolved_device,
        tokenizer_kwargs=tokenizer_kwargs or {"padding_side": "left"},
        trust_remote_code=True,
        model_kwargs=model_kwargs or None,
    )

    if resolved_device == "cpu":
        patch_gigar_embed_cpu_forward(st_model)

    return st_model

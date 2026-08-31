import torch


def resolve_torch_backend(device: str, *, purpose: str) -> str:
    if device == "cpu":
        return device
    if device == "cuda" and torch.cuda.is_available():
        return device
    mps_backend = getattr(torch.backends, "mps", None)
    if device == "mps" and mps_backend and mps_backend.is_available():
        return device
    raise RuntimeError(f"{purpose} device {device} was requested but is unavailable")

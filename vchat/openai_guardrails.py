from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from guardrails import GuardrailsAsyncOpenAI, GuardrailTripwireTriggered

from vchat.settings import config

logger = logging.getLogger("vchat.openai_guardrails")

_cached_client: GuardrailsAsyncOpenAI | None = None
_cached_key: tuple[str, str, str] | None = None


def _config_path() -> Path:
    raw = str(config.get("guardrails_config_path"))
    path = Path(raw)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent.parent / path
    return path


def get_guardrails_client(
    *, api_key: str, base_url: str
) -> GuardrailsAsyncOpenAI | None:
    if not bool(config.get("openai_guardrails_enabled", True)):
        return None

    cfg_path = _config_path()
    if not cfg_path.exists():
        logger.warning(
            "Guardrails config not found at '%s', guardrails disabled", cfg_path
        )
        return None

    global _cached_client, _cached_key
    key = (str(cfg_path), api_key, base_url)
    if _cached_client is not None and _cached_key == key:
        return _cached_client

    try:
        _cached_client = GuardrailsAsyncOpenAI(
            config=cfg_path,
            raise_guardrail_errors=False,
            api_key=api_key,
            base_url=base_url,
        )
    except Exception as exc:
        logger.warning("Failed to initialize GuardrailsAsyncOpenAI: %s", exc)
        return None

    _cached_key = key
    return _cached_client


def extract_tripwire_details(exc: GuardrailTripwireTriggered) -> tuple[str, str]:
    info: dict[str, Any] = (
        getattr(getattr(exc, "guardrail_result", None), "info", {}) or {}
    )

    stage = str(info.get("stage_name", "input")).strip().lower()
    if stage == "pre_flight":
        stage = "input"
    if stage not in {"input", "output"}:
        stage = "input"

    name = str(info.get("guardrail_name", "guardrail_tripwire")).strip().lower()
    name = re.sub(r"[^a-z0-9_]+", "_", name).strip("_")
    if not name:
        name = "guardrail_tripwire"

    return stage, name

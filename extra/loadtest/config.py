from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class IdleWsProfileConfig:
    base_url: str
    target_concurrency: int
    ramp_per_second: float
    hold_seconds: float
    connect_timeout_seconds: float
    heartbeat_seconds: float
    payload: str
    report_json_path: Path | None = None
    verify_ssl: bool = True
    receive_poll_seconds: float = 1.0
    sample_error_limit: int = 20

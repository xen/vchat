from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    if pct <= 0:
        return min(values)
    if pct >= 100:
        return max(values)
    ordered = sorted(values)
    pos = (len(ordered) - 1) * (pct / 100.0)
    lower = int(pos)
    upper = min(lower + 1, len(ordered) - 1)
    if lower == upper:
        return ordered[lower]
    fraction = pos - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


@dataclass(slots=True)
class IdleWsReport:
    profile: str
    base_url: str
    websocket_url: str
    target_concurrency: int
    ramp_per_second: float
    hold_seconds: float
    total_started: int
    total_connected: int
    total_connect_failed: int
    total_unexpected_disconnects: int
    max_active_connections: int
    elapsed_seconds: float
    connect_latency_ms: dict[str, float | None]
    session_duration_ms: dict[str, float | None]
    error_samples: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def summarize_latencies_ms(values: list[float]) -> dict[str, float | None]:
    return {
        "min": min(values) if values else None,
        "p50": _percentile(values, 50),
        "p95": _percentile(values, 95),
        "p99": _percentile(values, 99),
        "max": max(values) if values else None,
    }


def write_json_report(path: Path, report: IdleWsReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )


def render_report_text(report: IdleWsReport) -> str:
    lines = [
        f"profile: {report.profile}",
        f"websocket_url: {report.websocket_url}",
        f"target_concurrency: {report.target_concurrency}",
        f"started: {report.total_started}",
        f"connected: {report.total_connected}",
        f"connect_failed: {report.total_connect_failed}",
        f"unexpected_disconnects: {report.total_unexpected_disconnects}",
        f"max_active_connections: {report.max_active_connections}",
        f"elapsed_seconds: {report.elapsed_seconds:.2f}",
        (
            "connect_latency_ms: "
            f"p50={report.connect_latency_ms['p50']} "
            f"p95={report.connect_latency_ms['p95']} "
            f"p99={report.connect_latency_ms['p99']}"
        ),
        (
            "session_duration_ms: "
            f"p50={report.session_duration_ms['p50']} "
            f"p95={report.session_duration_ms['p95']} "
            f"p99={report.session_duration_ms['p99']}"
        ),
    ]
    if report.error_samples:
        lines.append("error_samples:")
        lines.extend(f"  - {sample}" for sample in report.error_samples)
    return "\n".join(lines)

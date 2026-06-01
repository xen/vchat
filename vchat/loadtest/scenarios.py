from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

import aiohttp

from .config import IdleWsProfileConfig
from .reporters import IdleWsReport, summarize_latencies_ms
from .websocket_client import ConnectionResult, hold_idle_websocket


@dataclass(slots=True)
class _ScenarioStats:
    started: int = 0
    connected: int = 0
    connect_failed: int = 0
    unexpected_disconnects: int = 0
    active_connections: int = 0
    max_active_connections: int = 0
    connect_latencies_ms: list[float] = field(default_factory=list)
    session_durations_ms: list[float] = field(default_factory=list)
    error_samples: list[str] = field(default_factory=list)


async def run_idle_ws_scenario(config: IdleWsProfileConfig) -> IdleWsReport:
    websocket_url = f"{config.base_url.rstrip('/')}/ws/chat/{config.payload}"
    connector = aiohttp.TCPConnector(limit=0, ssl=config.verify_ssl)
    timeout = aiohttp.ClientTimeout(
        total=None,
        connect=config.connect_timeout_seconds,
        sock_connect=config.connect_timeout_seconds,
    )
    stop_event = asyncio.Event()
    stats = _ScenarioStats()
    lock = asyncio.Lock()

    def _mark_connected() -> None:
        stats.connected += 1
        stats.active_connections += 1
        if stats.active_connections > stats.max_active_connections:
            stats.max_active_connections = stats.active_connections

    def _mark_disconnected() -> None:
        stats.active_connections = max(0, stats.active_connections - 1)

    async def _worker(index: int, session: aiohttp.ClientSession) -> ConnectionResult:
        delay = 0.0
        if config.ramp_per_second > 0:
            delay = index / config.ramp_per_second
        if delay > 0:
            await asyncio.sleep(delay)
        async with lock:
            stats.started += 1
        result = await hold_idle_websocket(
            session=session,
            websocket_url=websocket_url,
            heartbeat_seconds=config.heartbeat_seconds,
            receive_poll_seconds=config.receive_poll_seconds,
            stop_event=stop_event,
            on_connected=_mark_connected,
            on_disconnected=_mark_disconnected,
        )
        async with lock:
            if result.connected and result.connect_latency_ms is not None:
                stats.connect_latencies_ms.append(result.connect_latency_ms)
            if result.connected and result.session_duration_ms is not None:
                stats.session_durations_ms.append(result.session_duration_ms)
            if not result.connected:
                stats.connect_failed += 1
            if result.unexpected_disconnect:
                stats.unexpected_disconnects += 1
            if result.error and len(stats.error_samples) < config.sample_error_limit:
                stats.error_samples.append(result.error)
        return result

    started_at = time.perf_counter()
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        tasks = [
            asyncio.create_task(_worker(index, session))
            for index in range(config.target_concurrency)
        ]
        ramp_duration = (
            (config.target_concurrency - 1) / config.ramp_per_second
            if config.target_concurrency > 0 and config.ramp_per_second > 0
            else 0.0
        )
        await asyncio.sleep(ramp_duration + config.hold_seconds)
        stop_event.set()
        await asyncio.gather(*tasks)

    elapsed_seconds = time.perf_counter() - started_at
    return IdleWsReport(
        profile="idle_ws",
        base_url=config.base_url,
        websocket_url=websocket_url,
        target_concurrency=config.target_concurrency,
        ramp_per_second=config.ramp_per_second,
        hold_seconds=config.hold_seconds,
        total_started=stats.started,
        total_connected=stats.connected,
        total_connect_failed=stats.connect_failed,
        total_unexpected_disconnects=stats.unexpected_disconnects,
        max_active_connections=stats.max_active_connections,
        elapsed_seconds=elapsed_seconds,
        connect_latency_ms=summarize_latencies_ms(stats.connect_latencies_ms),
        session_duration_ms=summarize_latencies_ms(stats.session_durations_ms),
        error_samples=stats.error_samples,
    )

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass

import aiohttp


@dataclass(slots=True)
class ConnectionResult:
    connected: bool
    connect_latency_ms: float | None = None
    session_duration_ms: float | None = None
    error: str | None = None
    unexpected_disconnect: bool = False


async def hold_idle_websocket(
    *,
    session: aiohttp.ClientSession,
    websocket_url: str,
    heartbeat_seconds: float,
    receive_poll_seconds: float,
    stop_event: asyncio.Event,
    on_connected: Callable[[], None],
    on_disconnected: Callable[[], None],
) -> ConnectionResult:
    started_at = time.perf_counter()
    connected = False
    try:
        ws = await session.ws_connect(
            websocket_url,
            heartbeat=heartbeat_seconds,
            autoping=True,
            autoclose=True,
        )
        connected = True
        on_connected()
        connected_at = time.perf_counter()
        while not stop_event.is_set():
            try:
                msg = await ws.receive(timeout=receive_poll_seconds)
            except asyncio.TimeoutError:
                continue

            if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
                return ConnectionResult(
                    connected=True,
                    connect_latency_ms=(connected_at - started_at) * 1000.0,
                    session_duration_ms=(time.perf_counter() - connected_at) * 1000.0,
                    error="server_closed_connection",
                    unexpected_disconnect=True,
                )
            if msg.type == aiohttp.WSMsgType.ERROR:
                error = ws.exception()
                return ConnectionResult(
                    connected=True,
                    connect_latency_ms=(connected_at - started_at) * 1000.0,
                    session_duration_ms=(time.perf_counter() - connected_at) * 1000.0,
                    error=f"ws_error:{error}",
                    unexpected_disconnect=True,
                )

        await ws.close()
        return ConnectionResult(
            connected=True,
            connect_latency_ms=(connected_at - started_at) * 1000.0,
            session_duration_ms=(time.perf_counter() - connected_at) * 1000.0,
        )
    except Exception as exc:
        return ConnectionResult(
            connected=connected,
            connect_latency_ms=None,
            session_duration_ms=None,
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        if connected:
            on_disconnected()

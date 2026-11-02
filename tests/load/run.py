"""Run one fixed local scenario; start/stop only its own test processes."""

import argparse
import asyncio
import json
import math
import os
import platform
import resource
import subprocess
import sys
import time
from pathlib import Path

import aiohttp
import psutil
import sqlalchemy as sa
from itsdangerous import URLSafeSerializer

from tests.load.common import loopback_only, write_event
from tests.load.stand import validate_isolation
from vchat.db import async_session_factory
from vchat.settings import cfg


def percentiles(values):
    ordered = sorted(values)
    return {
        f"p{p}": ordered[max(0, math.ceil(len(ordered) * p / 100) - 1)]
        if ordered
        else None
        for p in (50, 90, 95)
    }


async def run(args):
    validate_isolation()
    if args.output.exists():
        raise RuntimeError("Output directory must be new; never overwrite measurements")
    args.output.mkdir(parents=True)
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (min(max(soft, 8192), hard), hard))
    children = []
    logs = []
    start = time.monotonic()
    stop_sampling = asyncio.Event()
    results = []
    sockets = []
    setup_errors = []
    ready = False
    client = aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit=0),
        timeout=aiohttp.ClientTimeout(total=120),
    )

    def spawn(module, arguments, label):
        log = (args.output / f"{label}.log").open("w")
        logs.append(log)
        process = subprocess.Popen(
            [sys.executable, "-m", module, *arguments],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        children.append(process)
        return process

    emulator = spawn(
        "tests.load.emulator",
        ["--events", str(args.output / "emulator.jsonl")],
        "emulator",
    )
    server = spawn(
        "tests.load.stand",
        ["serve", "--events", str(args.output / "server.jsonl")],
        "server",
    )

    async def sample():
        processes = [
            psutil.Process(pid)
            for pid in (os.getpid(), server.pid, emulator.pid, args.embedder_pid)
        ]
        while not stop_sampling.is_set():
            rows = []
            for process in processes:
                if process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
                    memory = process.memory_info()
                    rows.append(
                        {
                            "pid": process.pid,
                            "cpu_percent": process.cpu_percent(),
                            "rss": memory.rss,
                            "threads": process.num_threads(),
                            "fds": process.num_fds(),
                            "cpu_times": list(process.cpu_times()),
                        }
                    )
            write_event(
                args.output / "resources.jsonl",
                "sample",
                processes=rows,
                system_memory_available=psutil.virtual_memory().available,
                loadavg=os.getloadavg(),
            )
            await asyncio.sleep(0.5)

    sampler = asyncio.create_task(sample())
    try:
        for attempt in range(180):
            if server.poll() is not None or emulator.poll() is not None:
                raise RuntimeError("Test process exited during startup; see logs")
            try:
                async with client.get(
                    "http://127.0.0.1:18193/__load/stats"
                ) as response:
                    response.raise_for_status()
                    ready = True
                    break
            except aiohttp.ClientConnectorError:
                # Explicit startup polling, not a runtime dependency recovery path.
                await asyncio.sleep(1)
        if not ready:
            raise TimeoutError("Stand startup exceeded 180 seconds")
        total = 100 if args.scenario == "chat" else 1000
        semaphore = asyncio.Semaphore(50)
        serializer = URLSafeSerializer(cfg.secret_key)

        async def connect(ix):
            async with semaphore:
                try:
                    payload = serializer.dumps(
                        ["loadtest", f"loadtest-{ix:04d}", "loadtest"], salt="vchat"
                    )
                    ws = await client.ws_connect(
                        f"http://127.0.0.1:18193/ws/chat/{payload}"
                    )
                    sockets.append((ix, ws))
                    await ws.send_str("ping")
                    if await ws.receive_str(timeout=60) != "pong":
                        raise RuntimeError("Missing application pong")
                    write_event(args.output / "client.jsonl", "connected", ix=ix)
                except Exception as exc:
                    setup_errors.append({"ix": ix, "error": repr(exc)})
                    write_event(
                        args.output / "client.jsonl",
                        "connect_error",
                        ix=ix,
                        error=repr(exc),
                    )

        await asyncio.gather(*(connect(ix) for ix in range(total)))
        connected_at = time.monotonic()
        write_event(
            args.output / "client.jsonl",
            "all_connected",
            count=len(sockets),
            errors=len(setup_errors),
        )
        async with client.get("http://127.0.0.1:18193/__load/stats") as response:
            before = await response.json()
        async with async_session_factory() as db:
            before_rows = (
                await db.execute(
                    sa.text("select role, count(*) from chat_msg group by role")
                )
            ).all()
        if args.scenario == "connections":
            await asyncio.sleep(30)
        else:
            gate = asyncio.Event()

            async def message(ix, ws):
                await gate.wait()
                started = time.monotonic()
                first = None
                error = None
                final = None
                raw = []
                try:
                    await ws.send_str(
                        f"Как устроена учебная программа Команда {ix % 24} и регистрация участников?"
                    )
                    async with asyncio.timeout(120):
                        while True:
                            item = await ws.receive()
                            if item.type != aiohttp.WSMsgType.TEXT:
                                raise RuntimeError(f"WebSocket ended: {item.type.name}")
                            payload = json.loads(item.data)
                            raw.append(payload)
                            if (
                                payload.get("content")
                                and payload.get("partial")
                                and first is None
                            ):
                                first = time.monotonic() - started
                            if payload.get("error") or payload.get("ok") is False:
                                error = payload
                                break
                            if payload.get("partial") is False:
                                final = payload
                                if payload.get("guardrail"):
                                    error = {"guardrail": True}
                                break
                except Exception as exc:
                    error = {"exception": type(exc).__name__, "detail": str(exc)}
                result = {
                    "ix": ix,
                    "started": started,
                    "ended": time.monotonic(),
                    "first_seconds": first,
                    "full_seconds": time.monotonic() - started,
                    "error": error,
                    "final": final,
                    "frames": raw,
                }
                results.append(result)
                write_event(args.output / "responses.jsonl", "response", **result)

            tasks = [
                asyncio.create_task(message(ix, ws)) for ix, ws in sorted(sockets)[:100]
            ]
            gate.set()
            await asyncio.gather(*tasks)
            await asyncio.sleep(
                3
            )  # Observe post-response persistence/suggestions separately.

        # Prove sockets still usable, not merely successful upgrade handshakes.
        async def ping(ix, ws):
            await ws.send_str("ping")
            async with asyncio.timeout(30):
                while True:
                    value = await ws.receive_str()
                    if value == "pong":
                        write_event(args.output / "client.jsonl", "retained", ix=ix)
                        return True

        retained = await asyncio.gather(
            *(ping(ix, ws) for ix, ws in sockets), return_exceptions=True
        )
        async with client.get("http://127.0.0.1:18193/__load/stats") as response:
            after = await response.json()
        async with async_session_factory() as db:
            after_rows = (
                await db.execute(
                    sa.text("select role, count(*) from chat_msg group by role")
                )
            ).all()
        success = [row for row in results if row["error"] is None and row["final"]]
        summary = {
            "scenario": args.scenario,
            "code": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], text=True
            ).strip(),
            "dirty": subprocess.check_output(
                ["git", "status", "--porcelain"], text=True
            ),
            "platform": platform.platform(),
            "cpu_count": psutil.cpu_count(),
            "ram_bytes": psutil.virtual_memory().total,
            "python": sys.version,
            "duration_seconds": time.monotonic() - start,
            "connected": len(sockets),
            "connection_errors": setup_errors,
            "retained": sum(value is True for value in retained),
            "retention_errors": [
                repr(value) for value in retained if value is not True
            ],
            "hold_seconds": time.monotonic() - connected_at,
            "requests": len(results),
            "success": len(success),
            "errors": len(results) - len(success),
            "timeouts": sum(
                row["error"] is not None
                and row["error"].get("exception") == "TimeoutError"
                for row in results
            ),
            "first_content_seconds": percentiles(
                [
                    row["first_seconds"]
                    for row in success
                    if row["first_seconds"] is not None
                ]
            ),
            "full_success_seconds": percentiles(
                [row["full_seconds"] for row in success]
            ),
            "all_terminal_seconds": percentiles(
                [row["full_seconds"] for row in results]
            ),
            "server_before": before,
            "server_after": after,
            "db_before": [list(row) for row in before_rows],
            "db_after": [list(row) for row in after_rows],
        }
        (args.output / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
        )
        print(
            json.dumps(
                {
                    key: value
                    for key, value in summary.items()
                    if key not in ("dirty", "platform", "python")
                },
                ensure_ascii=False,
            )
        )
    finally:
        stop_sampling.set()
        await sampler
        # Current production websocket loop does not break on CLOSED frames.
        # Terminate our isolated processes before closing clients to avoid spinning
        # in that existing runtime loop. No application code is altered here.
        for process in reversed(children):
            process.terminate()
        await asyncio.sleep(1)
        for process in reversed(children):
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
        await client.close()
        for log in logs:
            log.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("scenario", choices=("connections", "chat", "combined"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--embedder-pid", type=int, required=True)
    args = parser.parse_args()
    loopback_only()
    asyncio.run(run(args))

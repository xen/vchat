from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from vchat.embeddings import resolve_embedding_device
from vchat.settings import config


THREAD_LIMIT_ENV = {
    "OMP_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
    "VECLIB_MAXIMUM_THREADS": "1",
    "TOKENIZERS_PARALLELISM": "false",
}


def _coerce_positive_int(value: str | int | None, default: int) -> int:
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    return max(1, parsed)


def _coerce_non_negative_int(value: str | int | None, default: int) -> int:
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    return max(0, parsed)


def resolve_embedder_instance_count(
    *,
    configured: str | int | None = None,
    cpu_count: int | None = None,
    reserve_cpus: int | None = None,
    device: str | None = None,
) -> int:
    configured_value = configured
    if configured_value is None:
        configured_value = os.getenv(
            "EMBEDDER_INSTANCES",
            config.get("embedding_worker_instances", "auto"),
        )
    if configured_value not in {None, "", "auto"}:
        return _coerce_positive_int(configured_value, 1)

    resolved_device = resolve_embedding_device(device)
    if resolved_device != "cpu":
        return 1

    total_cpus = max(1, int(cpu_count or os.cpu_count() or 1))
    reserve = _coerce_non_negative_int(
        reserve_cpus
        if reserve_cpus is not None
        else os.getenv(
            "EMBEDDER_CPU_RESERVE",
            config.get("embedding_worker_cpu_reserve", 1),
        ),
        1,
    )
    return max(1, total_cpus - reserve)


def _build_worker_name(hostname: str, index: int) -> str:
    return f"vchat-embedder-{hostname}-{os.getpid()}-{index}@{hostname}"


def main() -> int:
    project_root = Path(__file__).resolve().parents[2]
    celery_bin = project_root / "venv" / "bin" / "celery"
    pool = os.getenv("EMBEDDER_POOL", "solo")
    concurrency = os.getenv("EMBEDDER_CONCURRENCY", "1")
    hostname = socket.gethostname().split(".")[0] or "localhost"
    instance_count = resolve_embedder_instance_count()

    env = os.environ.copy()
    env.update({key: env.get(key, value) for key, value in THREAD_LIMIT_ENV.items()})
    env["EMBEDDER_POOL"] = pool
    env["EMBEDDER_CONCURRENCY"] = concurrency

    processes: list[subprocess.Popen[str]] = []
    stopping = False

    def _terminate_all(sig: int, _frame) -> None:
        nonlocal stopping
        if stopping:
            return
        stopping = True
        for proc in processes:
            if proc.poll() is None:
                proc.terminate()

    signal.signal(signal.SIGINT, _terminate_all)
    signal.signal(signal.SIGTERM, _terminate_all)

    print(
        f"Starting {instance_count} embedder worker(s) on {hostname} "
        f"(pool={pool}, concurrency={concurrency}, "
        f"device={os.getenv('EMBEDDING_DEVICE') or resolve_embedding_device()})"
    )
    for index in range(1, instance_count + 1):
        cmd = [
            str(celery_bin),
            "-A",
            "jobs.celery",
            "worker",
            "--loglevel=INFO",
            "-Q",
            "embeddings",
            f"--pool={pool}",
            f"--concurrency={concurrency}",
            "--max-tasks-per-child=1",
            "-n",
            _build_worker_name(hostname, index),
        ]
        proc = subprocess.Popen(cmd, cwd=project_root, env=env, text=True)
        processes.append(proc)

    exit_code = 0
    try:
        while processes:
            for proc in list(processes):
                code = proc.poll()
                if code is None:
                    continue
                processes.remove(proc)
                if not stopping and code != 0:
                    exit_code = code
                    _terminate_all(signal.SIGTERM, None)
            if processes:
                time.sleep(1)
    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.terminate()
        deadline = time.monotonic() + 10
        for proc in processes:
            if proc.poll() is None:
                timeout = max(0, deadline - time.monotonic())
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
        for proc in processes:
            if proc.poll() is None:
                proc.kill()

    return exit_code


if __name__ == "__main__":
    sys.exit(main())

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from jobs.embedder.model import resolve_embedding_device
from vchat.settings import cfg


THREAD_LIMIT_ENV = {
    "OMP_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
    "VECLIB_MAXIMUM_THREADS": "1",
    "TOKENIZERS_PARALLELISM": "false",
}


def resolve_embedder_instance_count() -> int:
    if isinstance(cfg.embedding_worker_instances, int):
        return cfg.embedding_worker_instances

    if resolve_embedding_device() != "cpu":
        return 1

    return max(1, (os.cpu_count() or 1) - cfg.embedding_worker_cpu_reserve)


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

    def _terminate_all(_ignore_sig: int, _frame) -> None:
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
        f"device={resolve_embedding_device()})"
    )
    for index in range(1, instance_count + 1):
        cmd = [
            str(celery_bin),
            "-A",
            "jobs.celery",
            "worker",
            "--include=jobs.embedder.tasks",
            "--loglevel=INFO",
            "-Q",
            "embeddings",
            f"--pool={pool}",
            f"--concurrency={concurrency}",
            "--max-tasks-per-child=1",
            "-n",
            f"vchat-embedder-{hostname}-{os.getpid()}-{index}@{hostname}",
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

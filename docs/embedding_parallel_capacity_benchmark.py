#!/usr/bin/env python
"""Find embedding throughput by parallel process count.

The benchmark uses multiple independent OS processes. It does not use Python
threads to increase model concurrency. Each worker loads its own
SentenceTransformer model and repeatedly encodes varied Russian texts up to the
configured maximum length.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import multiprocessing as mp
import os
import platform
import queue
import random
import signal
import statistics
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import psutil
import torch
from sentence_transformers import SentenceTransformer

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

TEXT_FRAGMENTS = [
    "Виртуальный ассистент индексирует базу знаний проекта и возвращает ответ с проверяемыми ссылками.",
    "Документ содержит правила эксплуатации, ограничения, таблицы параметров, примеры обращений и исключения.",
    "Пользователь задает вопрос, система выбирает релевантные фрагменты, нормализует текст и считает embedding.",
    "Качество retrieval зависит от формы chunk, длины входа, токенизации, дедупликации и ранжирования источников.",
    "Для нагрузочного теста важно разнообразить вход, чтобы не мерить один повторяющийся векторный шаблон.",
    "Служба обработки должна учитывать CPU, память, количество процессов, очередь и максимальный размер сообщения.",
    "Длинный HTML, PDF или таблица могут иметь другой профиль токенизации и создавать тяжелые batch payloads.",
]

DEFAULT_TEXT_SIZES = [256, 512, 1024, 2048, 3072, 4000]


@dataclass
class CapacityRow:
    host_label: str
    worker_count: int
    duration_seconds: float
    measured_wall_seconds: float
    total_embeddings: int
    total_chars: int
    total_tokens: int
    embeddings_per_second: float
    chars_per_second: float
    tokens_per_second: float
    mean_latency_seconds: float
    p50_latency_seconds: float
    p95_latency_seconds: float
    max_latency_seconds: float
    peak_total_rss_mb: float
    final_total_rss_mb: float
    mean_total_cpu_percent: float
    peak_total_cpu_percent: float
    worker_exit_count: int
    error_count: int


@dataclass
class MemoryGrowthRow:
    host_label: str
    worker_count: int
    sample_index: int
    elapsed_seconds: float
    total_embeddings: int
    total_rss_mb: float
    total_cpu_percent: float


def run_text(command: list[str]) -> str | None:
    try:
        return subprocess.check_output(command, text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return None


def hardware_info() -> dict[str, Any]:
    info: dict[str, Any] = {
        "hostname": run_text(["hostname"]),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "torch": torch.__version__,
        "cuda_available": torch.cuda.is_available(),
        "cpu_count_logical": psutil.cpu_count(logical=True),
        "cpu_count_physical": psutil.cpu_count(logical=False),
        "memory_total_bytes": psutil.virtual_memory().total,
    }
    lscpu = run_text(["lscpu"])
    if lscpu:
        parsed = {}
        for line in lscpu.splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            key = key.strip()
            if key in {
                "Architecture",
                "CPU(s)",
                "Model name",
                "Thread(s) per core",
                "Core(s) per socket",
                "Socket(s)",
                "CPU max MHz",
                "CPU min MHz",
                "NUMA node(s)",
                "L3 cache",
            }:
                parsed[key] = value.strip()
        info["lscpu"] = parsed
    return info


def make_text(size: int, index: int) -> str:
    rng = random.Random(index * 7919 + size)
    fragments = TEXT_FRAGMENTS[:]
    rng.shuffle(fragments)
    seed = f"Запрос {index}. " + " ".join(fragments) + " "
    while len(seed) < size:
        rng.shuffle(fragments)
        seed += " ".join(fragments) + " "
    return seed[:size].strip()


def text_plan(max_chars: int, plan_size: int) -> list[str]:
    sizes = [size for size in DEFAULT_TEXT_SIZES if size <= max_chars]
    if max_chars not in sizes:
        sizes.append(max_chars)
    return [make_text(sizes[i % len(sizes)], i) for i in range(plan_size)]


def worker_main(
    *,
    worker_id: int,
    model_name: str,
    max_chars: int,
    plan_size: int,
    device: str,
    stop_event: mp.Event,
    ready_queue: mp.Queue,
    result_queue: mp.Queue,
) -> None:
    try:
        torch.set_num_threads(1)
        torch.set_num_interop_threads(1)
    except Exception:
        pass

    model = SentenceTransformer(model_name, device=device, trust_remote_code=True)
    model.max_seq_length = 8192
    texts = text_plan(max_chars=max_chars, plan_size=plan_size)
    token_counts = []
    for text in texts:
        encoded = model.tokenizer(
            text,
            add_special_tokens=False,
            truncation=False,
            return_attention_mask=False,
        )
        token_counts.append(len(encoded["input_ids"]))
    ready_queue.put({"worker_id": worker_id, "pid": os.getpid(), "status": "ready"})

    count = 0
    total_chars = 0
    total_tokens = 0
    latencies: list[float] = []
    errors: list[str] = []
    started = time.perf_counter()
    while not stop_event.is_set():
        idx = count % len(texts)
        text = texts[idx]
        try:
            before = time.perf_counter()
            model.encode(
                [text],
                normalize_embeddings=True,
                batch_size=1,
                show_progress_bar=False,
            )
            if device == "mps" and torch.backends.mps.is_available():
                torch.mps.synchronize()
            latency = time.perf_counter() - before
            latencies.append(latency)
            count += 1
            total_chars += len(text)
            total_tokens += token_counts[idx]
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            if len(errors) >= 3:
                break

    result_queue.put(
        {
            "worker_id": worker_id,
            "pid": os.getpid(),
            "elapsed_seconds": time.perf_counter() - started,
            "count": count,
            "total_chars": total_chars,
            "total_tokens": total_tokens,
            "latencies": latencies[-1000:],
            "errors": errors,
        }
    )


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, math.ceil((pct / 100) * len(ordered)) - 1)
    return ordered[idx]


def sample_processes(
    processes: list[mp.Process],
    *,
    cpu_interval: float | None = None,
) -> tuple[float, float]:
    total_rss = 0.0
    total_cpu = 0.0
    for proc in processes:
        if proc.pid is None:
            continue
        try:
            ps_proc = psutil.Process(proc.pid)
            total_rss += ps_proc.memory_info().rss / (1024 * 1024)
            total_cpu += ps_proc.cpu_percent(interval=cpu_interval)
        except psutil.Error:
            continue
    return total_rss, total_cpu


def run_capacity(
    *,
    host_label: str,
    model_name: str,
    device: str,
    worker_count: int,
    duration_seconds: float,
    max_chars: int,
    plan_size: int,
    sample_interval: float,
) -> tuple[CapacityRow, list[MemoryGrowthRow]]:
    ctx = mp.get_context("spawn")
    stop_event = ctx.Event()
    result_queue: mp.Queue = ctx.Queue()
    ready_queue: mp.Queue = ctx.Queue()
    processes = [
        ctx.Process(
            target=worker_main,
            kwargs={
                "worker_id": idx,
                "model_name": model_name,
                "device": device,
                "max_chars": max_chars,
                "plan_size": plan_size,
                "stop_event": stop_event,
                "ready_queue": ready_queue,
                "result_queue": result_queue,
            },
        )
        for idx in range(worker_count)
    ]
    for proc in processes:
        proc.start()

    ready = 0
    ready_deadline = time.perf_counter() + 600
    while ready < worker_count and time.perf_counter() < ready_deadline:
        try:
            message = ready_queue.get(timeout=5)
            if message.get("status") == "ready":
                ready += 1
        except queue.Empty:
            if any(proc.exitcode not in (None, 0) for proc in processes):
                break

    # Initialize psutil CPU accounting.
    sample_processes(processes, cpu_interval=None)
    started = time.perf_counter()
    samples: list[MemoryGrowthRow] = []
    peak_rss = 0.0
    peak_cpu = 0.0
    cpu_values: list[float] = []
    sample_index = 0
    while time.perf_counter() - started < duration_seconds:
        time.sleep(sample_interval)
        rss, cpu = sample_processes(processes, cpu_interval=0.02)
        peak_rss = max(peak_rss, rss)
        peak_cpu = max(peak_cpu, cpu)
        cpu_values.append(cpu)
        samples.append(
            MemoryGrowthRow(
                host_label=host_label,
                worker_count=worker_count,
                sample_index=sample_index,
                elapsed_seconds=time.perf_counter() - started,
                total_embeddings=0,
                total_rss_mb=rss,
                total_cpu_percent=cpu,
            )
        )
        sample_index += 1

    stop_event.set()
    for proc in processes:
        proc.join(timeout=90)
    for proc in processes:
        if proc.is_alive():
            os.kill(proc.pid or 0, signal.SIGTERM)
            proc.join(timeout=10)

    results = []
    while True:
        try:
            results.append(result_queue.get_nowait())
        except queue.Empty:
            break

    total_embeddings = sum(item.get("count", 0) for item in results)
    total_chars = sum(item.get("total_chars", 0) for item in results)
    total_tokens = sum(item.get("total_tokens", 0) for item in results)
    latencies = [
        latency
        for item in results
        for latency in item.get("latencies", [])
    ]
    errors = [
        error
        for item in results
        for error in item.get("errors", [])
    ]
    measured_wall = time.perf_counter() - started
    final_rss, final_cpu = sample_processes(processes, cpu_interval=0.02)

    # Attribute final processed count to the last memory sample for trend plots.
    if samples:
        for idx, sample in enumerate(samples):
            estimated = int(total_embeddings * ((idx + 1) / len(samples)))
            sample.total_embeddings = estimated

    row = CapacityRow(
        host_label=host_label,
        worker_count=worker_count,
        duration_seconds=duration_seconds,
        measured_wall_seconds=measured_wall,
        total_embeddings=total_embeddings,
        total_chars=total_chars,
        total_tokens=total_tokens,
        embeddings_per_second=total_embeddings / measured_wall if measured_wall else 0.0,
        chars_per_second=total_chars / measured_wall if measured_wall else 0.0,
        tokens_per_second=total_tokens / measured_wall if measured_wall else 0.0,
        mean_latency_seconds=statistics.mean(latencies) if latencies else 0.0,
        p50_latency_seconds=statistics.median(latencies) if latencies else 0.0,
        p95_latency_seconds=percentile(latencies, 95),
        max_latency_seconds=max(latencies) if latencies else 0.0,
        peak_total_rss_mb=peak_rss,
        final_total_rss_mb=final_rss,
        mean_total_cpu_percent=statistics.mean(cpu_values) if cpu_values else 0.0,
        peak_total_cpu_percent=max(peak_cpu, final_cpu),
        worker_exit_count=sum(1 for proc in processes if proc.exitcode is not None),
        error_count=len(errors),
    )
    return row, samples


def parse_worker_counts(raw: str) -> list[int]:
    return sorted({int(value) for value in raw.replace(",", " ").split() if value.strip()})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host-label", required=True)
    parser.add_argument("--model", default="deepvk/USER-bge-m3")
    parser.add_argument("--device", choices=["cpu", "mps"], default="cpu")
    parser.add_argument("--output-dir", default=".")
    parser.add_argument("--worker-counts", required=True)
    parser.add_argument("--duration-seconds", type=float, default=90.0)
    parser.add_argument("--max-chars", type=int, default=4000)
    parser.add_argument("--plan-size", type=int, default=96)
    parser.add_argument("--sample-interval", type=float, default=2.0)
    args = parser.parse_args()
    if args.device == "mps" and not torch.backends.mps.is_available():
        raise RuntimeError("MPS device was requested but torch.backends.mps.is_available() is false")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    worker_counts = parse_worker_counts(args.worker_counts)

    rows: list[CapacityRow] = []
    memory_rows: list[MemoryGrowthRow] = []
    for worker_count in worker_counts:
        row, samples = run_capacity(
            host_label=args.host_label,
            model_name=args.model,
            device=args.device,
            worker_count=worker_count,
            duration_seconds=args.duration_seconds,
            max_chars=args.max_chars,
            plan_size=args.plan_size,
            sample_interval=args.sample_interval,
        )
        rows.append(row)
        memory_rows.extend(samples)
        print(
            f"{args.host_label} workers={worker_count} "
            f"eps={row.embeddings_per_second:.3f} "
            f"rss_peak_mb={row.peak_total_rss_mb:.0f} "
            f"cpu_mean={row.mean_total_cpu_percent:.0f}",
            flush=True,
        )

    capacity_csv = output_dir / f"{args.host_label}_parallel_capacity.csv"
    memory_csv = output_dir / f"{args.host_label}_parallel_memory_samples.csv"
    payload_json = output_dir / f"{args.host_label}_parallel_capacity.json"

    with capacity_csv.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(asdict(rows[0]).keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))

    with memory_csv.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(asdict(memory_rows[0]).keys()))
        writer.writeheader()
        for row in memory_rows:
            writer.writerow(asdict(row))

    payload_json.write_text(
        json.dumps(
            {
                "environment": hardware_info(),
                "method": {
                    "model": args.model,
                    "device": args.device,
                    "concurrency": "parallel OS processes, one model per process",
                    "max_chars": args.max_chars,
                    "plan_size": args.plan_size,
                    "duration_seconds": args.duration_seconds,
                    "sample_interval": args.sample_interval,
                    "worker_counts": worker_counts,
                    "torch_threads_per_process": 1,
                    "input": "varied synthetic Russian texts cycling through 256..max_chars",
                },
                "capacity_rows": [asdict(row) for row in rows],
                "memory_samples": [asdict(row) for row in memory_rows],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    print(f"wrote {capacity_csv}")
    print(f"wrote {memory_csv}")
    print(f"wrote {payload_json}")


if __name__ == "__main__":
    main()

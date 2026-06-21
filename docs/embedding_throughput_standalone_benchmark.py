#!/usr/bin/env python
"""Standalone CPU embedding benchmark for remote hosts.

Unlike docs/embedding_throughput_benchmark.py, this script does not import the
vchat codebase. It is meant to be copied into a temporary server directory,
run against a downloaded SentenceTransformer model, and then removed together
with that temporary directory.
"""

from __future__ import annotations

import argparse
import csv
import gc
import json
import math
import os
import platform
import statistics
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import psutil
import torch
from sentence_transformers import SentenceTransformer

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

DEFAULT_SIZES = [256, 512, 1024, 2048, 4096, 8192, 12000]
TEXT_SEED = (
    "Виртуальный ассистент индексирует базу знаний проекта, извлекает релевантные "
    "фрагменты документации, нормализует текст, строит эмбеддинги и возвращает "
    "ответ с проверяемыми ссылками на источники. "
)


@dataclass
class BenchmarkRow:
    host_label: str
    device: str
    requested_chars: int
    actual_chars: int
    token_count: int
    repeat_count: int
    min_seconds: float
    median_seconds: float
    mean_seconds: float
    max_seconds: float
    messages_per_second_median: float
    chars_per_second_median: float
    tokens_per_second_median: float
    rss_before_mb: float
    rss_after_mb: float


def run_text(command: list[str]) -> str | None:
    try:
        return subprocess.check_output(command, text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host-label", required=True)
    parser.add_argument("--model", default="deepvk/USER-bge-m3")
    parser.add_argument("--device", default="cpu", choices=["cpu", "cuda"])
    parser.add_argument("--output-dir", default=".")
    parser.add_argument("--sizes", nargs="+", type=int, default=DEFAULT_SIZES)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--warmups", type=int, default=1)
    return parser.parse_args()


def make_text(size: int) -> str:
    return (TEXT_SEED * math.ceil(size / len(TEXT_SEED)))[:size].strip()


def sync_device(device: str) -> None:
    if device == "cuda":
        torch.cuda.synchronize()


def rss_mb() -> float:
    return psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)


def token_count(model: SentenceTransformer, text: str) -> int:
    encoded = model.tokenizer(
        text,
        add_special_tokens=False,
        truncation=False,
        return_attention_mask=False,
    )
    return len(encoded["input_ids"])


def hardware_info() -> dict[str, object]:
    info: dict[str, object] = {
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


def benchmark(args: argparse.Namespace) -> tuple[list[BenchmarkRow], dict[str, object]]:
    if args.device == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA requested but unavailable")

    model = SentenceTransformer(args.model, device=args.device, trust_remote_code=True)
    model.max_seq_length = 8192
    rows: list[BenchmarkRow] = []
    try:
        for size in args.sizes:
            text = make_text(size)
            tokens = token_count(model, text)
            for _ in range(args.warmups):
                model.encode(
                    [text],
                    normalize_embeddings=True,
                    batch_size=1,
                    show_progress_bar=False,
                )
                sync_device(args.device)

            timings = []
            before = rss_mb()
            for _ in range(args.repeats):
                sync_device(args.device)
                started = time.perf_counter()
                model.encode(
                    [text],
                    normalize_embeddings=True,
                    batch_size=1,
                    show_progress_bar=False,
                )
                sync_device(args.device)
                timings.append(time.perf_counter() - started)
            after = rss_mb()
            median_seconds = statistics.median(timings)
            rows.append(
                BenchmarkRow(
                    host_label=args.host_label,
                    device=args.device,
                    requested_chars=size,
                    actual_chars=len(text),
                    token_count=tokens,
                    repeat_count=args.repeats,
                    min_seconds=min(timings),
                    median_seconds=median_seconds,
                    mean_seconds=statistics.mean(timings),
                    max_seconds=max(timings),
                    messages_per_second_median=1 / median_seconds,
                    chars_per_second_median=len(text) / median_seconds,
                    tokens_per_second_median=tokens / median_seconds,
                    rss_before_mb=before,
                    rss_after_mb=after,
                )
            )
            print(
                f"{args.host_label} {args.device} size={size:>5} chars "
                f"tokens={tokens:>5} median={median_seconds:.3f}s",
                flush=True,
            )
    finally:
        if hasattr(model, "cpu"):
            model.cpu()
        del model
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    return rows, hardware_info()


def main() -> None:
    args = parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    rows, hw = benchmark(args)

    csv_path = output_dir / f"{args.host_label}_embedding_benchmark.csv"
    json_path = output_dir / f"{args.host_label}_embedding_benchmark.json"
    with csv_path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(asdict(rows[0]).keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))
    json_path.write_text(
        json.dumps(
            {
                "environment": hw,
                "method": {
                    "model": args.model,
                    "device": args.device,
                    "input": "single synthetic Russian message encoded with batch_size=1",
                    "sizes": args.sizes,
                    "repeats": args.repeats,
                    "warmups": args.warmups,
                    "metric": "median wall-clock seconds after warmup",
                },
                "rows": [asdict(row) for row in rows],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    print(f"wrote {csv_path}")
    print(f"wrote {json_path}")


if __name__ == "__main__":
    main()

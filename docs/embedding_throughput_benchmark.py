#!/usr/bin/env python
"""Benchmark vchat embedding model latency by input size on CPU and MPS.

This script is intentionally self-contained so the resulting report can be
reproduced without touching application runtime code or Celery workers.
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
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import psutil
import torch

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from jobs.embedder.model import load_embedding_model, release_torch_cache
from vchat.settings import config


OUTPUT_DIR = Path("tmp/embedding_throughput_benchmark/results")
CSV_PATH = OUTPUT_DIR / "embedding_cpu_mps_by_message_size.csv"
JSON_PATH = OUTPUT_DIR / "embedding_cpu_mps_by_message_size.json"
SVG_PATH = OUTPUT_DIR / "embedding_cpu_mps_by_message_size.svg"
PNG_PATH = OUTPUT_DIR / "embedding_cpu_mps_by_message_size.png"

DEFAULT_SIZES = [256, 512, 1024, 2048, 4096, 8192, 12000]
TEXT_SEED = (
    "Виртуальный ассистент индексирует базу знаний проекта, извлекает релевантные "
    "фрагменты документации, нормализует текст, строит эмбеддинги и возвращает "
    "ответ с проверяемыми ссылками на источники. "
)


@dataclass
class BenchmarkRow:
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--devices",
        nargs="+",
        default=["cpu", "mps"],
        choices=["cpu", "mps"],
        help="Devices to benchmark.",
    )
    parser.add_argument(
        "--sizes",
        nargs="+",
        type=int,
        default=DEFAULT_SIZES,
        help="Message sizes in characters.",
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=3,
        help="Measured repeats per device and size after warmup.",
    )
    parser.add_argument(
        "--warmups",
        type=int,
        default=1,
        help="Warmup repeats per device and size.",
    )
    return parser.parse_args()


def make_text(size: int) -> str:
    repeated = (TEXT_SEED * math.ceil(size / len(TEXT_SEED)))[:size]
    return repeated.strip()


def sync_device(device: str) -> None:
    if device == "mps" and hasattr(torch, "mps"):
        torch.mps.synchronize()
    elif device == "cuda":
        torch.cuda.synchronize()


def device_available(device: str) -> bool:
    if device == "cpu":
        return True
    if device == "mps":
        mps_backend = getattr(torch.backends, "mps", None)
        return bool(mps_backend and mps_backend.is_available())
    return False


def rss_mb() -> float:
    return psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)


def token_count(model, text: str) -> int:
    encoded = model.tokenizer(
        text,
        add_special_tokens=False,
        truncation=False,
        return_attention_mask=False,
    )
    return len(encoded["input_ids"])


def benchmark_device(
    *,
    device: str,
    sizes: list[int],
    repeats: int,
    warmups: int,
) -> list[BenchmarkRow]:
    if not device_available(device):
        print(f"skip device={device}: unavailable")
        return []

    print(f"loading model on {device}")
    model = load_embedding_model(device=device)
    rows: list[BenchmarkRow] = []

    try:
        for size in sizes:
            text = make_text(size)
            tokens = token_count(model, text)
            for _ in range(warmups):
                model.encode(
                    [text],
                    normalize_embeddings=True,
                    batch_size=1,
                    show_progress_bar=False,
                )
                sync_device(device)

            timings: list[float] = []
            before = rss_mb()
            for _ in range(repeats):
                sync_device(device)
                started = time.perf_counter()
                model.encode(
                    [text],
                    normalize_embeddings=True,
                    batch_size=1,
                    show_progress_bar=False,
                )
                sync_device(device)
                timings.append(time.perf_counter() - started)
            after = rss_mb()

            median_seconds = statistics.median(timings)
            rows.append(
                BenchmarkRow(
                    device=device,
                    requested_chars=size,
                    actual_chars=len(text),
                    token_count=tokens,
                    repeat_count=repeats,
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
                f"{device:>3} size={size:>5} chars tokens={tokens:>5} "
                f"median={median_seconds:.3f}s"
            )
    finally:
        if hasattr(model, "cpu"):
            model.cpu()
        del model
        gc.collect()
        release_torch_cache()

    return rows


def write_csv(rows: list[BenchmarkRow]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with CSV_PATH.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(asdict(rows[0]).keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


def write_json(rows: list[BenchmarkRow], args: argparse.Namespace) -> None:
    payload = {
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "torch": torch.__version__,
            "mps_available": device_available("mps"),
            "cuda_available": torch.cuda.is_available(),
            "embedding_model_id": config.get("embedding_model_id"),
            "embedding_model_dir": config.get("embedding_model_dir"),
            "embedding_max_seq_length": config.get("embedding_max_seq_length"),
            "embedding_chunk_max_chars": config.get("embedding_chunk_max_chars"),
            "embedding_encode_batch_max_chars": config.get(
                "embedding_encode_batch_max_chars"
            ),
        },
        "method": {
            "input": "single synthetic Russian message encoded with batch_size=1",
            "sizes": args.sizes,
            "repeats": args.repeats,
            "warmups": args.warmups,
            "metric": "median wall-clock seconds after warmup",
        },
        "rows": [asdict(row) for row in rows],
    }
    JSON_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def line_points(
    rows: list[BenchmarkRow],
    *,
    device: str,
    metric: str,
    x_min: float,
    x_max: float,
    y_min: float,
    y_max: float,
    left: float,
    top: float,
    width: float,
    height: float,
) -> str:
    points = []
    for row in sorted((r for r in rows if r.device == device), key=lambda r: r.actual_chars):
        x_ratio = (row.actual_chars - x_min) / (x_max - x_min)
        y_value = getattr(row, metric)
        y_ratio = (y_value - y_min) / (y_max - y_min)
        x = left + x_ratio * width
        y = top + height - y_ratio * height
        points.append(f"{x:.1f},{y:.1f}")
    return " ".join(points)


def write_svg(rows: list[BenchmarkRow]) -> None:
    width = 1100
    height = 680
    left = 110
    top = 92
    plot_width = 860
    plot_height = 430
    x_values = [row.actual_chars for row in rows]
    y_values = [row.median_seconds for row in rows]
    x_min = 0
    x_max = max(x_values) * 1.03
    y_min = 0
    y_max = max(y_values) * 1.12
    colors = {"cpu": "#5477C4", "mps": "#CC6F47"}
    labels = {"cpu": "CPU", "mps": "MPS"}

    grid_lines = []
    for tick in [0, 0.25, 0.5, 0.75, 1.0]:
        y = top + plot_height - tick * plot_height
        value = y_min + tick * (y_max - y_min)
        grid_lines.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_width}" y2="{y:.1f}" '
            'stroke="#E6E8F0" stroke-width="1"/>'
        )
        grid_lines.append(
            f'<text x="{left - 14}" y="{y + 4:.1f}" text-anchor="end" '
            'font-family="Menlo, Consolas, monospace" font-size="13" fill="#6F768A">'
            f"{value:.2f}</text>"
        )

    x_ticks = [0, 2000, 4000, 8000, 12000]
    x_tick_nodes = []
    for value in x_ticks:
        if value > x_max:
            continue
        x = left + ((value - x_min) / (x_max - x_min)) * plot_width
        x_tick_nodes.append(
            f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{top + plot_height}" '
            'stroke="#F4F5F7" stroke-width="1"/>'
        )
        x_tick_nodes.append(
            f'<text x="{x:.1f}" y="{top + plot_height + 30}" text-anchor="middle" '
            'font-family="Menlo, Consolas, monospace" font-size="13" fill="#6F768A">'
            f"{value:,}</text>"
        )

    series_nodes = []
    for device in sorted({row.device for row in rows}):
        points = line_points(
            rows,
            device=device,
            metric="median_seconds",
            x_min=x_min,
            x_max=x_max,
            y_min=y_min,
            y_max=y_max,
            left=left,
            top=top,
            width=plot_width,
            height=plot_height,
        )
        series_nodes.append(
            f'<polyline fill="none" stroke="{colors[device]}" stroke-width="3" '
            f'stroke-linejoin="round" stroke-linecap="round" points="{points}"/>'
        )
        for row in sorted((r for r in rows if r.device == device), key=lambda r: r.actual_chars):
            x = left + ((row.actual_chars - x_min) / (x_max - x_min)) * plot_width
            y = top + plot_height - (
                (row.median_seconds - y_min) / (y_max - y_min)
            ) * plot_height
            series_nodes.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="#FFFFFF" '
                f'stroke="{colors[device]}" stroke-width="3"/>'
            )

    legend_nodes = []
    legend_x = left + plot_width - 130
    for idx, device in enumerate(sorted({row.device for row in rows})):
        y = top - 28 + idx * 26
        legend_nodes.append(
            f'<line x1="{legend_x}" y1="{y}" x2="{legend_x + 26}" y2="{y}" '
            f'stroke="{colors[device]}" stroke-width="3" stroke-linecap="round"/>'
        )
        legend_nodes.append(
            f'<text x="{legend_x + 36}" y="{y + 5}" font-family="Inter, Arial, sans-serif" '
            f'font-size="15" fill="#1F2430">{labels[device]}</text>'
        )

    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
  <rect width="100%" height="100%" fill="#FCFCFD"/>
  <rect x="{left}" y="{top}" width="{plot_width}" height="{plot_height}" fill="#FFFFFF"/>
  <text x="{left}" y="42" font-family="Inter, Arial, sans-serif" font-size="24" font-weight="700" fill="#1F2430">Embedding latency grows with message size</text>
  <text x="{left}" y="68" font-family="Inter, Arial, sans-serif" font-size="15" fill="#6F768A">Median seconds per single-message encode after warmup; lower is better.</text>
  {''.join(grid_lines)}
  {''.join(x_tick_nodes)}
  <line x1="{left}" y1="{top + plot_height}" x2="{left + plot_width}" y2="{top + plot_height}" stroke="#D7DBE7" stroke-width="1.5"/>
  <line x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_height}" stroke="#D7DBE7" stroke-width="1.5"/>
  {''.join(series_nodes)}
  {''.join(legend_nodes)}
  <text x="{left + plot_width / 2}" y="{top + plot_height + 66}" text-anchor="middle" font-family="Inter, Arial, sans-serif" font-size="15" fill="#1F2430">Message length, characters</text>
  <text x="28" y="{top + plot_height / 2}" text-anchor="middle" transform="rotate(-90 28 {top + plot_height / 2})" font-family="Inter, Arial, sans-serif" font-size="15" fill="#1F2430">Median latency, seconds</text>
  <text x="{left}" y="{height - 48}" font-family="Inter, Arial, sans-serif" font-size="13" fill="#6F768A">Model: {config.get("embedding_model_id")} from {config.get("embedding_model_dir")}; batch_size=1; synthetic Russian text.</text>
</svg>
"""
    SVG_PATH.write_text(svg)


def main() -> None:
    args = parse_args()
    rows: list[BenchmarkRow] = []
    for device in args.devices:
        rows.extend(
            benchmark_device(
                device=device,
                sizes=args.sizes,
                repeats=args.repeats,
                warmups=args.warmups,
            )
        )

    if not rows:
        raise RuntimeError("No benchmark rows were produced")

    write_csv(rows)
    write_json(rows, args)
    write_svg(rows)
    print(f"wrote {CSV_PATH}")
    print(f"wrote {JSON_PATH}")
    print(f"wrote {SVG_PATH}")


if __name__ == "__main__":
    main()

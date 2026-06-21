#!/usr/bin/env python
"""Build combined embedding throughput report assets from local and server runs."""

from __future__ import annotations

import csv
import json
import platform
import subprocess
from pathlib import Path
from typing import Any


BASE_DIR = Path("tmp/embedding_throughput_benchmark/results")
LOCAL_CSV = BASE_DIR / "embedding_cpu_mps_by_message_size.csv"
LOCAL_JSON = BASE_DIR / "embedding_cpu_mps_by_message_size.json"
SERVER_DIR = BASE_DIR / "server_runs"
COMBINED_CSV = BASE_DIR / "embedding_all_hosts_by_message_size.csv"
COMBINED_JSON = BASE_DIR / "embedding_all_hosts_by_message_size.json"
SERVER_CHART_SVG = BASE_DIR / "embedding_all_hosts_latency.svg"
SERVER_CHART_PNG = BASE_DIR / "embedding_all_hosts_latency.png"
PARALLEL_DIR = BASE_DIR / "parallel_runs"
PARALLEL_COMBINED_CSV = BASE_DIR / "embedding_parallel_capacity_all.csv"
PARALLEL_COMBINED_JSON = BASE_DIR / "embedding_parallel_capacity_all.json"
PARALLEL_THROUGHPUT_SVG = BASE_DIR / "embedding_parallel_throughput_by_cpu.svg"
PARALLEL_RSS_SVG = BASE_DIR / "embedding_parallel_rss_by_cpu.svg"
PARALLEL_MEMORY_SVG = BASE_DIR / "embedding_parallel_memory_growth.svg"


SERIES_LABELS = {
    "local-m2-max/cpu": "Apple M2 Max CPU",
    "local-m2-max/mps": "Apple M2 Max MPS",
    "cdn-okumy/cpu": "Intel i7-3770 3.4GHz CPU",
    "bear-infraforecast/cpu": "Threadripper 2950X 3.5GHz CPU",
    "trade-infraforecast/cpu": "EPYC 7401P 2.0GHz CPU",
}
SERIES_COLORS = {
    "local-m2-max/cpu": "#5477C4",
    "local-m2-max/mps": "#CC6F47",
    "cdn-okumy/cpu": "#7A828F",
    "bear-infraforecast/cpu": "#71B436",
    "trade-infraforecast/cpu": "#BD569B",
}
CPU_LABELS_BY_HOST = {
    "local-m2-max": "Apple M2 Max CPU",
    "local-m2-max-mps": "Apple M2 Max MPS",
    "cdn-okumy": "Intel i7-3770 3.4GHz CPU",
    "bear-infraforecast": "Threadripper 2950X 3.5GHz CPU",
    "trade-infraforecast": "EPYC 7401P 2.0GHz CPU",
}
CPU_COLORS_BY_HOST = {
    "local-m2-max": "#5477C4",
    "local-m2-max-mps": "#CC6F47",
    "cdn-okumy": "#7A828F",
    "bear-infraforecast": "#71B436",
    "trade-infraforecast": "#BD569B",
}


def run_text(command: list[str]) -> str | None:
    try:
        return subprocess.check_output(command, text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return None


def local_hardware() -> dict[str, Any]:
    cpu = run_text(["sysctl", "-n", "machdep.cpu.brand_string"])
    physical_cpu = run_text(["sysctl", "-n", "hw.physicalcpu"])
    logical_cpu = run_text(["sysctl", "-n", "hw.logicalcpu"])
    mem_bytes = run_text(["sysctl", "-n", "hw.memsize"])
    return {
        "hostname": run_text(["hostname"]),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "cpu_model": cpu,
        "cpu_count_physical": int(physical_cpu) if physical_cpu else None,
        "cpu_count_logical": int(logical_cpu) if logical_cpu else None,
        "memory_total_bytes": int(mem_bytes) if mem_bytes else None,
        "memory_total_gb": round(int(mem_bytes) / 1024 / 1024 / 1024, 2)
        if mem_bytes
        else None,
        "gpu_or_accelerator": "Apple M2 Max integrated GPU via PyTorch MPS",
    }


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def combined_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in read_csv(LOCAL_CSV):
        host_label = "local-m2-max"
        rows.append(
            {
                **row,
                "host_label": host_label,
                "series": f"{host_label}/{row['device']}",
                "scope": "local baseline",
            }
        )

    for path in sorted(SERVER_DIR.glob("*_embedding_benchmark.csv")):
        for row in read_csv(path):
            rows.append({**row, "series": f"{row['host_label']}/{row['device']}", "scope": "server run"})

    numeric_fields = {
        "requested_chars": int,
        "actual_chars": int,
        "token_count": int,
        "repeat_count": int,
        "min_seconds": float,
        "median_seconds": float,
        "mean_seconds": float,
        "max_seconds": float,
        "messages_per_second_median": float,
        "chars_per_second_median": float,
        "tokens_per_second_median": float,
        "rss_before_mb": float,
        "rss_after_mb": float,
    }
    for row in rows:
        for key, caster in numeric_fields.items():
            row[key] = caster(row[key])
    return sorted(rows, key=lambda r: (r["series"], r["actual_chars"]))


def write_combined_outputs(rows: list[dict[str, Any]]) -> None:
    fieldnames = [
        "scope",
        "host_label",
        "device",
        "series",
        "requested_chars",
        "actual_chars",
        "token_count",
        "repeat_count",
        "min_seconds",
        "median_seconds",
        "mean_seconds",
        "max_seconds",
        "messages_per_second_median",
        "chars_per_second_median",
        "tokens_per_second_median",
        "rss_before_mb",
        "rss_after_mb",
    ]
    with COMBINED_CSV.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    local_payload = read_json(LOCAL_JSON)
    server_payloads = {
        path.stem.replace("_embedding_benchmark", ""): read_json(path)
        for path in sorted(SERVER_DIR.glob("*_embedding_benchmark.json"))
    }
    COMBINED_JSON.write_text(
        json.dumps(
            {
                "local_hardware": local_hardware(),
                "local_environment": local_payload.get("environment", {}),
                "server_environments": {
                    key: value.get("environment", {})
                    for key, value in server_payloads.items()
                },
                "method": {
                    "model": "deepvk/USER-bge-m3",
                    "input": "single synthetic Russian message encoded with batch_size=1",
                    "sizes": [256, 512, 1024, 2048, 4096, 8192, 12000],
                    "repeats": 3,
                    "warmups": 1,
                    "metric": "median wall-clock seconds after warmup",
                    "note": "Local run includes CPU and MPS. Server runs are CPU-only.",
                },
                "rows": rows,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


def point_for(
    row: dict[str, Any],
    *,
    x_max: float,
    y_max: float,
    left: float,
    top: float,
    width: float,
    height: float,
) -> tuple[float, float]:
    x = left + (row["actual_chars"] / x_max) * width
    y = top + height - (row["median_seconds"] / y_max) * height
    return x, y


def write_chart(rows: list[dict[str, Any]]) -> None:
    width = 1480
    height = 760
    left = 110
    top = 98
    plot_width = 860
    plot_height = 470
    x_max = max(row["actual_chars"] for row in rows) * 1.03
    y_max = max(row["median_seconds"] for row in rows) * 1.12

    grid_nodes = []
    for tick in [0, 0.25, 0.5, 0.75, 1.0]:
        y = top + plot_height - tick * plot_height
        value = tick * y_max
        grid_nodes.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_width}" y2="{y:.1f}" '
            'stroke="#E6E8F0" stroke-width="1"/>'
        )
        grid_nodes.append(
            f'<text x="{left - 14}" y="{y + 4:.1f}" text-anchor="end" '
            'font-family="Menlo, Consolas, monospace" font-size="13" fill="#6F768A">'
            f"{value:.1f}</text>"
        )

    x_nodes = []
    for value in [0, 2000, 4000, 8000, 12000]:
        x = left + (value / x_max) * plot_width
        x_nodes.append(
            f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{top + plot_height}" '
            'stroke="#F4F5F7" stroke-width="1"/>'
        )
        x_nodes.append(
            f'<text x="{x:.1f}" y="{top + plot_height + 30}" text-anchor="middle" '
            'font-family="Menlo, Consolas, monospace" font-size="13" fill="#6F768A">'
            f"{value:,}</text>"
        )

    series_nodes = []
    legend_nodes = []
    for idx, series in enumerate(SERIES_LABELS):
        series_rows = sorted(
            [row for row in rows if row["series"] == series],
            key=lambda row: row["actual_chars"],
        )
        if not series_rows:
            continue
        points = []
        for row in series_rows:
            x, y = point_for(
                row,
                x_max=x_max,
                y_max=y_max,
                left=left,
                top=top,
                width=plot_width,
                height=plot_height,
            )
            points.append(f"{x:.1f},{y:.1f}")
        color = SERIES_COLORS[series]
        series_nodes.append(
            f'<polyline fill="none" stroke="{color}" stroke-width="3" '
            f'stroke-linejoin="round" stroke-linecap="round" points="{" ".join(points)}"/>'
        )
        for row in series_rows:
            x, y = point_for(
                row,
                x_max=x_max,
                y_max=y_max,
                left=left,
                top=top,
                width=plot_width,
                height=plot_height,
            )
            series_nodes.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4.6" fill="#FFFFFF" '
                f'stroke="{color}" stroke-width="2.6"/>'
            )
        legend_y = top + idx * 28
        legend_x = left + plot_width + 34
        legend_nodes.append(
            f'<line x1="{legend_x}" y1="{legend_y}" x2="{legend_x + 26}" y2="{legend_y}" '
            f'stroke="{color}" stroke-width="3" stroke-linecap="round"/>'
        )
        legend_nodes.append(
            f'<text x="{legend_x + 36}" y="{legend_y + 5}" font-family="Inter, Arial, sans-serif" '
            f'font-size="14" fill="#1F2430">{SERIES_LABELS[series]}</text>'
        )

    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
  <rect width="100%" height="100%" fill="#FCFCFD"/>
  <rect x="{left}" y="{top}" width="{plot_width}" height="{plot_height}" fill="#FFFFFF"/>
  <text x="{left}" y="42" font-family="Inter, Arial, sans-serif" font-size="24" font-weight="700" fill="#1F2430">Embedding latency by CPU type and message size</text>
  <text x="{left}" y="70" font-family="Inter, Arial, sans-serif" font-size="15" fill="#6F768A">Median seconds per single-message encode after warmup; lower is better. Server runs are CPU-only.</text>
  {''.join(grid_nodes)}
  {''.join(x_nodes)}
  <line x1="{left}" y1="{top + plot_height}" x2="{left + plot_width}" y2="{top + plot_height}" stroke="#D7DBE7" stroke-width="1.5"/>
  <line x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_height}" stroke="#D7DBE7" stroke-width="1.5"/>
  {''.join(series_nodes)}
  {''.join(legend_nodes)}
  <text x="{left + plot_width / 2}" y="{top + plot_height + 66}" text-anchor="middle" font-family="Inter, Arial, sans-serif" font-size="15" fill="#1F2430">Message length, characters</text>
  <text x="28" y="{top + plot_height / 2}" text-anchor="middle" transform="rotate(-90 28 {top + plot_height / 2})" font-family="Inter, Arial, sans-serif" font-size="15" fill="#1F2430">Median latency, seconds</text>
  <text x="{left}" y="{height - 52}" font-family="Inter, Arial, sans-serif" font-size="13" fill="#6F768A">Model: deepvk/USER-bge-m3; batch_size=1; synthetic Russian text; repeats=3 after one warmup.</text>
</svg>
"""
    SERVER_CHART_SVG.write_text(svg)


def read_parallel_capacity() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(PARALLEL_DIR.glob("*_parallel_capacity.csv")):
        for row in read_csv(path):
            rows.append(
                {
                    **row,
                    "cpu_label": CPU_LABELS_BY_HOST.get(row["host_label"], row["host_label"]),
                }
            )
    numeric_fields = {
        "worker_count": int,
        "duration_seconds": float,
        "measured_wall_seconds": float,
        "total_embeddings": int,
        "total_chars": int,
        "total_tokens": int,
        "embeddings_per_second": float,
        "chars_per_second": float,
        "tokens_per_second": float,
        "mean_latency_seconds": float,
        "p50_latency_seconds": float,
        "p95_latency_seconds": float,
        "max_latency_seconds": float,
        "peak_total_rss_mb": float,
        "final_total_rss_mb": float,
        "mean_total_cpu_percent": float,
        "peak_total_cpu_percent": float,
        "worker_exit_count": int,
        "error_count": int,
    }
    for row in rows:
        for key, caster in numeric_fields.items():
            row[key] = caster(row[key])
    return sorted(rows, key=lambda row: (row["host_label"], row["worker_count"]))


def read_parallel_memory() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(PARALLEL_DIR.glob("*_parallel_memory_samples.csv")):
        for row in read_csv(path):
            rows.append(
                {
                    **row,
                    "cpu_label": CPU_LABELS_BY_HOST.get(row["host_label"], row["host_label"]),
                }
            )
    numeric_fields = {
        "worker_count": int,
        "sample_index": int,
        "elapsed_seconds": float,
        "total_embeddings": int,
        "total_rss_mb": float,
        "total_cpu_percent": float,
    }
    for row in rows:
        for key, caster in numeric_fields.items():
            row[key] = caster(row[key])
    return sorted(rows, key=lambda row: (row["host_label"], row["worker_count"], row["sample_index"]))


def optimal_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary = []
    for host in sorted({row["host_label"] for row in rows}):
        host_rows = [row for row in rows if row["host_label"] == host]
        best = max(host_rows, key=lambda row: row["embeddings_per_second"])
        threshold = best["embeddings_per_second"] * 0.9
        zone = [
            row["worker_count"]
            for row in host_rows
            if row["embeddings_per_second"] >= threshold
        ]
        summary.append(
            {
                "host_label": host,
                "cpu_label": CPU_LABELS_BY_HOST.get(host, host),
                "best_worker_count": best["worker_count"],
                "best_embeddings_per_second": best["embeddings_per_second"],
                "best_peak_rss_mb": best["peak_total_rss_mb"],
                "best_mean_cpu_percent": best["mean_total_cpu_percent"],
                "best_p50_latency_seconds": best["p50_latency_seconds"],
                "best_p95_latency_seconds": best["p95_latency_seconds"],
                "near_peak_worker_zone": f"{min(zone)}-{max(zone)}" if zone else "",
            }
        )
    return summary


def write_parallel_outputs(rows: list[dict[str, Any]], memory_rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    with PARALLEL_COMBINED_CSV.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    payloads = {
        path.stem.replace("_parallel_capacity", ""): read_json(path)
        for path in sorted(PARALLEL_DIR.glob("*_parallel_capacity.json"))
    }
    PARALLEL_COMBINED_JSON.write_text(
        json.dumps(
            {
                "method": {
                    "concurrency": "parallel OS processes, one model per process, torch threads per process set to 1",
                    "max_chars": 4000,
                    "duration_seconds": 90,
                    "input": "varied synthetic Russian texts cycling through 256..4000 chars",
                },
                "optimal_summary": optimal_summary(rows),
                "environments": {
                    key: value.get("environment", {})
                    for key, value in payloads.items()
                },
                "capacity_rows": rows,
                "memory_samples": memory_rows,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


def chart_xy(
    rows: list[dict[str, Any]],
    *,
    x_field: str,
    y_field: str,
    title: str,
    subtitle: str,
    x_axis: str,
    y_axis: str,
    output: Path,
    x_ticks: list[int] | None = None,
) -> None:
    width = 1480
    height = 740
    left = 110
    top = 98
    plot_width = 850
    plot_height = 450
    x_max = max(row[x_field] for row in rows) * 1.05
    y_max = max(row[y_field] for row in rows) * 1.15

    grid_nodes = []
    for tick in [0, 0.25, 0.5, 0.75, 1.0]:
        y = top + plot_height - tick * plot_height
        value = tick * y_max
        grid_nodes.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_width}" y2="{y:.1f}" stroke="#E6E8F0" stroke-width="1"/>'
        )
        grid_nodes.append(
            f'<text x="{left - 14}" y="{y + 4:.1f}" text-anchor="end" font-family="Menlo, Consolas, monospace" font-size="13" fill="#6F768A">{value:.1f}</text>'
        )
    x_nodes = []
    if x_ticks is None:
        x_ticks = [0, 4, 8, 16, 24, 32, 48]
    for value in x_ticks:
        if value > x_max:
            continue
        x = left + (value / x_max) * plot_width
        x_nodes.append(
            f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{top + plot_height}" stroke="#F4F5F7" stroke-width="1"/>'
        )
        x_nodes.append(
            f'<text x="{x:.1f}" y="{top + plot_height + 30}" text-anchor="middle" font-family="Menlo, Consolas, monospace" font-size="13" fill="#6F768A">{value}</text>'
        )

    series_nodes = []
    legend_nodes = []
    for idx, host in enumerate(CPU_LABELS_BY_HOST):
        host_rows = sorted(
            [row for row in rows if row["host_label"] == host],
            key=lambda row: row[x_field],
        )
        if not host_rows:
            continue
        color = CPU_COLORS_BY_HOST[host]
        points = []
        for row in host_rows:
            x = left + (row[x_field] / x_max) * plot_width
            y = top + plot_height - (row[y_field] / y_max) * plot_height
            points.append(f"{x:.1f},{y:.1f}")
        series_nodes.append(
            f'<polyline fill="none" stroke="{color}" stroke-width="3" stroke-linejoin="round" stroke-linecap="round" points="{" ".join(points)}"/>'
        )
        for row in host_rows:
            x = left + (row[x_field] / x_max) * plot_width
            y = top + plot_height - (row[y_field] / y_max) * plot_height
            series_nodes.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4.6" fill="#FFFFFF" stroke="{color}" stroke-width="2.6"/>'
            )
        legend_x = left + plot_width + 34
        legend_y = top + idx * 28
        legend_nodes.append(
            f'<line x1="{legend_x}" y1="{legend_y}" x2="{legend_x + 26}" y2="{legend_y}" stroke="{color}" stroke-width="3" stroke-linecap="round"/>'
        )
        legend_nodes.append(
            f'<text x="{legend_x + 36}" y="{legend_y + 5}" font-family="Inter, Arial, sans-serif" font-size="14" fill="#1F2430">{CPU_LABELS_BY_HOST[host]}</text>'
        )

    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
  <rect width="100%" height="100%" fill="#FCFCFD"/>
  <rect x="{left}" y="{top}" width="{plot_width}" height="{plot_height}" fill="#FFFFFF"/>
  <text x="{left}" y="42" font-family="Inter, Arial, sans-serif" font-size="24" font-weight="700" fill="#1F2430">{title}</text>
  <text x="{left}" y="70" font-family="Inter, Arial, sans-serif" font-size="15" fill="#6F768A">{subtitle}</text>
  {''.join(grid_nodes)}
  {''.join(x_nodes)}
  <line x1="{left}" y1="{top + plot_height}" x2="{left + plot_width}" y2="{top + plot_height}" stroke="#D7DBE7" stroke-width="1.5"/>
  <line x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_height}" stroke="#D7DBE7" stroke-width="1.5"/>
  {''.join(series_nodes)}
  {''.join(legend_nodes)}
  <text x="{left + plot_width / 2}" y="{top + plot_height + 66}" text-anchor="middle" font-family="Inter, Arial, sans-serif" font-size="15" fill="#1F2430">{x_axis}</text>
  <text x="28" y="{top + plot_height / 2}" text-anchor="middle" transform="rotate(-90 28 {top + plot_height / 2})" font-family="Inter, Arial, sans-serif" font-size="15" fill="#1F2430">{y_axis}</text>
  <text x="{left}" y="{height - 52}" font-family="Inter, Arial, sans-serif" font-size="13" fill="#6F768A">Model: deepvk/USER-bge-m3; each process loads one model; torch threads per process = 1; varied texts up to 4000 chars.</text>
</svg>
"""
    output.write_text(svg)


def write_memory_growth_chart(rows: list[dict[str, Any]], memory_rows: list[dict[str, Any]]) -> None:
    best_by_host = {
        item["host_label"]: item["best_worker_count"]
        for item in optimal_summary(rows)
    }
    selected = [
        row
        for row in memory_rows
        if row["worker_count"] == best_by_host.get(row["host_label"])
    ]
    if not selected:
        return
    chart_xy(
        selected,
        x_field="total_embeddings",
        y_field="total_rss_mb",
        title="Peak-zone RSS while embeddings accumulate",
        subtitle="Memory samples from the best-throughput worker count; x-axis uses completed embeddings estimated over the run.",
        x_axis="Completed embeddings during run",
        y_axis="Total worker RSS, MB",
        output=PARALLEL_MEMORY_SVG,
        x_ticks=[0, 100, 200, 300, 400, 500],
    )


def main() -> None:
    rows = combined_rows()
    write_combined_outputs(rows)
    write_chart(rows)
    parallel_rows = read_parallel_capacity()
    memory_rows = read_parallel_memory()
    write_parallel_outputs(parallel_rows, memory_rows)
    if parallel_rows:
        chart_xy(
            parallel_rows,
            x_field="worker_count",
            y_field="embeddings_per_second",
            title="Parallel embedding throughput by runtime type",
            subtitle="Throughput from independent worker processes; CPU-thread cap is one per process.",
            x_axis="Parallel worker processes",
            y_axis="Embeddings per second",
            output=PARALLEL_THROUGHPUT_SVG,
        )
        chart_xy(
            parallel_rows,
            x_field="worker_count",
            y_field="peak_total_rss_mb",
            title="Parallel embedding memory by runtime type",
            subtitle="Peak total RSS across embedding worker processes during each run.",
            x_axis="Parallel worker processes",
            y_axis="Peak total RSS, MB",
            output=PARALLEL_RSS_SVG,
        )
        write_memory_growth_chart(parallel_rows, memory_rows)
    print(f"wrote {COMBINED_CSV}")
    print(f"wrote {COMBINED_JSON}")
    print(f"wrote {SERVER_CHART_SVG}")


if __name__ == "__main__":
    main()

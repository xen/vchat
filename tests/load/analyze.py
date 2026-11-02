"""Recompute evidence from raw artifacts; never turn absent measurements into zero."""

import argparse
import collections
import json
from pathlib import Path

from tests.load.run import percentiles


def read_rows(path):
    return (
        [json.loads(line) for line in path.read_text().splitlines()]
        if path.exists()
        else []
    )


def analyze(directory: Path):
    server = read_rows(directory / "server.jsonl")
    responses = read_rows(directory / "responses.jsonl")
    emulator = read_rows(directory / "emulator.jsonl")
    resources = read_rows(directory / "resources.jsonl")
    starts = [row for row in server if row["event"] == "request_start"]
    ends = [row for row in server if row["event"] == "request_end"]
    runtime = [row for row in server if row["event"] == "runtime_sample"]
    if starts and ends:
        runtime = [
            row
            for row in runtime
            if min(item["t"] for item in starts)
            <= row["t"]
            <= max(item["t"] for item in ends)
        ]
    successful = [row for row in responses if row["error"] is None and row["final"]]
    stage_names = sorted({row["name"] for row in server if row["event"] == "stage"})
    active_streams = 0
    peak_streams = 0
    for row in emulator:
        if row["event"] == "completion_start" and row["stream"] is True:
            active_streams += 1
            peak_streams = max(peak_streams, active_streams)
        elif row["event"] == "completion_end":
            active_streams -= 1
    roles = ("client", "vchat", "emulator", "embedder")
    process_ids = [row["pid"] for row in resources[0]["processes"]] if resources else []
    result = {
        "server_requests_started": len(starts),
        "server_requests_ended": len(ends),
        "server_peak_active": max((row["active"] for row in starts), default=None),
        "all_100_overlap_seconds": min(row["t"] for row in ends)
        - max(row["t"] for row in starts)
        if len(starts) == 100 and ends
        else None,
        "peak_provider_streams": peak_streams,
        "errors": dict(
            collections.Counter(
                row["error"].get("error", row["error"].get("exception", "guardrail"))
                for row in responses
                if row["error"]
            )
        ),
        "unsupported_provider_protocols": dict(
            collections.Counter(
                row["path"] for row in emulator if row["event"] == "unsupported"
            )
        ),
        "stream_completed": sum(row["event"] == "completion_end" for row in emulator),
        "transport_completed": len(successful),
        "full_protected_cycle_validated": False,
        "completed_with_1984_chars": sum(
            len(
                "".join(
                    frame.get("content", "")
                    for frame in row["frames"]
                    if frame.get("partial")
                )
            )
            == 1984
            for row in successful
        ),
        "source_counts": dict(
            collections.Counter(str(len(row["final"]["sources"])) for row in successful)
        ),
        "eventloop_lag_seconds": percentiles(
            [row["eventloop_lag_seconds"] for row in runtime]
        ),
        "eventloop_lag_max_seconds": max(
            (row["eventloop_lag_seconds"] for row in runtime), default=None
        ),
        "db_peak_checkedout": max(
            (row["db_checkedout"] for row in runtime), default=None
        ),
        "stages_ms": {
            name: percentiles(
                [
                    row["ms"]
                    for row in server
                    if row["event"] == "stage" and row["name"] == name
                ]
            )
            for name in stage_names
        },
        "embedding_queue_seconds": percentiles(
            [
                row["seconds"]
                for row in server
                if row["event"] == "record_request_embedding_queue_wait"
            ]
        ),
        "embedding_encode_seconds": percentiles(
            [
                row["seconds"]
                for row in server
                if row["event"] == "record_request_embedding_encode"
            ]
        ),
        "rerank_ms": percentiles(
            [row["ms"] for row in server if row["event"] == "rerank"]
        ),
        "resources": {
            role: {
                "pid": pid,
                "peak_rss_bytes": max(
                    row["rss"]
                    for sample in resources
                    for row in sample["processes"]
                    if row["pid"] == pid
                ),
                "peak_cpu_percent": max(
                    row["cpu_percent"]
                    for sample in resources
                    for row in sample["processes"]
                    if row["pid"] == pid
                ),
            }
            for role, pid in zip(roles, process_ids, strict=True)
        },
        "system_loadavg_max_1m": max(
            (row["loadavg"][0] for row in resources), default=None
        ),
        "system_available_ram_min_bytes": min(
            (row["system_memory_available"] for row in resources), default=None
        ),
    }
    (directory / "derived.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    print(json.dumps(analyze(args.directory), ensure_ascii=False, indent=2))

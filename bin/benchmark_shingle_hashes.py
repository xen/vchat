from __future__ import annotations

import argparse
import hashlib
import importlib.util
import statistics
import time
from collections.abc import Callable


def to_signed_64(value: int) -> int:
    return value - (1 << 64) if value >= (1 << 63) else value


def build_values(count: int) -> list[str]:
    return [f"alpha beta gamma {i} delta epsilon zeta" for i in range(count)]


def benchmark(
    values: list[str], repeats: int, fn: Callable[[str], int]
) -> tuple[float, float, float]:
    samples: list[float] = []
    for _ in range(repeats):
        start = time.perf_counter()
        for value in values:
            fn(value)
        samples.append(time.perf_counter() - start)
    avg_ms = statistics.mean(samples) * 1000
    min_ms = min(samples) * 1000
    per_item_us = statistics.mean(samples) / len(values) * 1_000_000
    return avg_ms, min_ms, per_item_us


def standard_benchmarks() -> list[tuple[str, Callable[[str], int]]]:
    return [
        (
            "blake2s_8bytes",
            lambda text: int.from_bytes(
                hashlib.blake2s(text.encode("utf-8"), digest_size=8).digest(),
                byteorder="big",
                signed=True,
            ),
        ),
        (
            "blake2b_8bytes",
            lambda text: int.from_bytes(
                hashlib.blake2b(text.encode("utf-8"), digest_size=8).digest(),
                byteorder="big",
                signed=True,
            ),
        ),
        (
            "sha256_8bytes",
            lambda text: int.from_bytes(
                hashlib.sha256(text.encode("utf-8")).digest()[:8],
                byteorder="big",
                signed=True,
            ),
        ),
        (
            "md5_8bytes",
            lambda text: int.from_bytes(
                hashlib.md5(text.encode("utf-8"), usedforsecurity=False).digest()[:8],
                byteorder="big",
                signed=True,
            ),
        ),
    ]


def optional_benchmarks() -> list[tuple[str, Callable[[str], int]]]:
    items: list[tuple[str, Callable[[str], int]]] = []

    if importlib.util.find_spec("xxhash"):
        import xxhash

        items.append(
            (
                "xxh64_intdigest",
                lambda text: to_signed_64(xxhash.xxh64_intdigest(text.encode("utf-8"))),
            )
        )

    if importlib.util.find_spec("blake3"):
        import blake3

        items.append(
            (
                "blake3_8bytes",
                lambda text: int.from_bytes(
                    blake3.blake3(text.encode("utf-8")).digest(length=8),
                    byteorder="big",
                    signed=True,
                ),
            )
        )

    if importlib.util.find_spec("mmh3"):
        import mmh3

        items.append(("mmh3_hash64", lambda text: mmh3.hash64(text, signed=True)[0]))

    return items


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark shingle hash functions")
    parser.add_argument("--count", type=int, default=10_000)
    parser.add_argument("--repeats", type=int, default=7)
    args = parser.parse_args()

    values = build_values(args.count)
    candidates = standard_benchmarks() + optional_benchmarks()
    results: list[tuple[str, float, float, float]] = []

    for name, fn in candidates:
        avg_ms, min_ms, per_item_us = benchmark(values, args.repeats, fn)
        results.append((name, avg_ms, min_ms, per_item_us))

    for name, avg_ms, min_ms, per_item_us in sorted(results, key=lambda item: item[1]):
        print(
            f"{name:16} avg={avg_ms:.2f} ms  min={min_ms:.2f} ms  per_item={per_item_us:.2f} us"
        )


if __name__ == "__main__":
    main()

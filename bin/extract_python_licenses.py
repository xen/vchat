#!/usr/bin/env python3
"""Extract license metadata from installed Python packages.

Reads JSON package list from stdin, outputs JSON mapping of licenses found in
installed package metadata.

Input: [{"name": "package_name", "version": "version_string"}, ...]
Output: {"package_name@@version_string": ["license1", "license2"], ...}
"""

import importlib.metadata as md
import json
import re
import sys


def norm(name: str) -> str:
    """Normalize package name for consistent lookup."""
    return re.sub(r"[-_.]+", "-", name).lower().strip()


def extract_licenses(meta) -> list[str]:
    """Extract license information from package metadata.

    Checks both the License field and License classifiers.
    """
    licenses = set()
    raw = (meta.get("License") or "").strip()
    if raw and raw.upper() != "UNKNOWN":
        licenses.add(raw)
    for classifier in meta.get_all("Classifier") or []:
        if classifier.startswith("License ::"):
            parts = [p.strip() for p in classifier.split("::") if p.strip()]
            if parts:
                licenses.add(" / ".join(parts))
    return sorted(licenses)


def main() -> int:
    """Main entry point."""
    targets = json.loads(sys.stdin.read() or "[]")

    # Build distribution map from installed packages
    dist_map = {}
    for dist in md.distributions():
        dist_name = dist.metadata.get("Name")
        if not dist_name:
            continue
        dist_map.setdefault(norm(dist_name), []).append(dist)

    # Lookup licenses for each target package
    result = {}
    for item in targets:
        name = item.get("name", "")
        version = item.get("version", "")
        key = f"{name}@@{version}"
        matches = dist_map.get(norm(name), [])
        chosen = None

        # Try exact version match first
        if version:
            for dist in matches:
                if getattr(dist, "version", "") == version:
                    chosen = dist
                    break

        # Fall back to any available version
        if chosen is None and matches:
            chosen = matches[0]

        if chosen is None:
            result[key] = []
            continue

        licenses = extract_licenses(chosen.metadata)
        result[key] = licenses

    sys.stdout.write(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

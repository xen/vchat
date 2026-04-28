#!/usr/bin/env python3
"""Build a readable HTML report from security scanner artifacts.

Usage:
  python3 bin/security-report.py --security-dir security --output security/security-report.html
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SEVERITY_ORDER = {
    "CRITICAL": 0,
    "HIGH": 1,
    "MODERATE": 2,
    "MEDIUM": 2,
    "LOW": 3,
    "UNKNOWN": 4,
}

WEIGHTS = {
    "CRITICAL": 5,
    "HIGH": 3,
    "MODERATE": 2,
    "MEDIUM": 2,
    "LOW": 1,
    "UNKNOWN": 1,
}


def load_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text())
    except Exception:
        return fallback


def load_exitcode(security_dir: Path, name: str) -> int | None:
    path = security_dir / f"{name}.exitcode"
    if not path.exists():
        return None
    try:
        return int(path.read_text().strip())
    except ValueError:
        return None


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def run_git(*args: str) -> str:
    try:
        return subprocess.check_output(["git", *args], text=True).strip()
    except Exception:
        return ""


def parse_proof(path: Path) -> dict[str, str]:
    proof: dict[str, str] = {}
    if not path.exists():
        return proof
    for line in path.read_text().splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        proof[key.strip()] = value.strip()
    return proof


def esc(value: Any) -> str:
    return html.escape(str(value))


def normalize_package_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower().strip()


def find_python_executable(project_root: Path) -> Path:
    venv_python = project_root / "venv" / "bin" / "python"
    if venv_python.exists():
        return venv_python
    return Path(sys.executable)


def collect_python_metadata_licenses(
    python_executable: Path,
    packages: list[dict[str, str]],
) -> dict[tuple[str, str], set[str]]:
    if not packages:
        return {}

    payload = [
        {"name": pkg.get("name", ""), "version": pkg.get("version", "")}
        for pkg in packages
        if pkg.get("name")
    ]
    if not payload:
        return {}

    # Run extract_python_licenses.py script to get license metadata
    extractor_script = Path(__file__).parent / "extract_python_licenses.py"
    if not extractor_script.exists():
        return {}

    try:
        completed = subprocess.run(
            [str(python_executable), str(extractor_script)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            check=False,
        )
    except Exception:
        return {}

    if completed.returncode != 0:
        return {}

    try:
        raw = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        return {}

    mapped: dict[tuple[str, str], set[str]] = {}
    for pkg in payload:
        key = f"{pkg['name']}@@{pkg['version']}"
        licenses = {str(item).strip() for item in raw.get(key, []) if str(item).strip()}
        mapped[(pkg["name"], pkg["version"])] = licenses
    return mapped


def table(headers: list[str], rows: list[list[Any]]) -> str:
    header_html = "".join(f"<th>{esc(h)}</th>" for h in headers)
    if rows:
        body = "".join(
            "<tr>" + "".join(f"<td>{esc(col)}</td>" for col in row) + "</tr>"
            for row in rows
        )
    else:
        body = '<tr><td colspan="99">No findings</td></tr>'
    return f"<table><thead><tr>{header_html}</tr></thead><tbody>{body}</tbody></table>"


def generate_security_artifacts(security_dir: Path) -> None:
    started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    finished_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sast = load_json(security_dir / "sast-semgrep.gitlab.json", {})
    osv = load_json(security_dir / "dependency-audit-osv.json", {})
    sbom = load_json(security_dir / "sbom.cyclonedx.json", {})
    ruff = load_json(security_dir / "lint-ruff.json", [])
    bandit = load_json(security_dir / "sast-bandit.json", {})

    licenses: dict[str, list[dict[str, str]]] = {}
    python_packages: list[dict[str, str]] = []
    for component in sbom.get("components", []):
        name = component.get("name") or component.get("bom-ref") or "unknown"
        version = component.get("version") or ""
        purl = (component.get("purl") or "").lower()
        package_entry = {
            "name": name,
            "version": version,
            "type": component.get("type") or "",
        }

        if purl.startswith("pkg:pypi/"):
            python_packages.append(package_entry)

        for license_entry in component.get("licenses", []) or []:
            license_data = license_entry.get("license", {})
            license_id = (
                license_data.get("id")
                or license_data.get("name")
                or license_entry.get("expression")
                or "unknown"
            )
            licenses.setdefault(license_id, []).append(package_entry)

    license_report = {
        "generated_at": finished_at,
        "source": "sbom.cyclonedx.json",
        "license_count": len(licenses),
        "licenses": [
            {"license": key, "packages": value}
            for key, value in sorted(licenses.items(), key=lambda item: item[0])
        ],
    }
    (security_dir / "license-summary.json").write_text(
        json.dumps(license_report, indent=2, sort_keys=True) + "\n"
    )

    python_license_map: dict[str, list[dict[str, str]]] = {}
    package_to_licenses: dict[tuple[str, str], set[str]] = {}
    license_items = license_report.get("licenses", [])
    if isinstance(license_items, list):
        for license_item in license_items:
            if not isinstance(license_item, dict):
                continue
            license_id = str(license_item.get("license") or "unknown")
            packages = license_item.get("packages", [])
            if not isinstance(packages, list):
                continue
            for pkg in packages:
                if not isinstance(pkg, dict):
                    continue
                pkg_key = (
                    str(pkg.get("name") or "unknown"),
                    str(pkg.get("version") or ""),
                )
                package_to_licenses.setdefault(pkg_key, set()).add(license_id)

    python_executable = find_python_executable(security_dir.parent)
    metadata_licenses = collect_python_metadata_licenses(
        python_executable, python_packages
    )

    for pkg in python_packages:
        pkg_key = (pkg["name"], pkg["version"])
        sbom_licenses = package_to_licenses.get(pkg_key, set())
        merged = (sbom_licenses - {"unknown"}) | metadata_licenses.get(pkg_key, set())
        effective = merged or (sbom_licenses or {"unknown"})
        for license_id in sorted(effective):
            python_license_map.setdefault(license_id, []).append(pkg)

    python_license_report = {
        "generated_at": finished_at,
        "source": "sbom.cyclonedx.json + python metadata",
        "package_count": len({(p["name"], p["version"]) for p in python_packages}),
        "license_count": len(python_license_map),
        "licenses": [
            {"license": key, "packages": value}
            for key, value in sorted(
                python_license_map.items(), key=lambda item: item[0]
            )
        ],
    }
    (security_dir / "python-license-summary.json").write_text(
        json.dumps(python_license_report, indent=2, sort_keys=True) + "\n"
    )

    summary = {
        "generated_at": finished_at,
        "checks": {
            "sast-semgrep": {
                "exit_code": load_exitcode(security_dir, "sast-semgrep"),
                "report": "sast-semgrep.gitlab.json",
                "format": "gitlab-sast-json",
                "findings": len(sast.get("vulnerabilities", [])),
            },
            "secret-detection-gitleaks": {
                "exit_code": load_exitcode(security_dir, "secret-detection-gitleaks"),
                "report": "secret-detection-gitleaks.sarif",
                "format": "sarif",
            },
            "dependency-audit-osv": {
                "exit_code": load_exitcode(security_dir, "dependency-audit-osv"),
                "report": "dependency-audit-osv.json",
                "format": "osv-json",
                "results": len(osv.get("results", [])),
            },
            "sbom-cyclonedx": {
                "exit_code": load_exitcode(security_dir, "sbom-syft-cyclonedx"),
                "report": "sbom.cyclonedx.json",
                "format": "cyclonedx-json",
                "components": len(sbom.get("components", [])),
            },
            "sbom-spdx": {
                "exit_code": load_exitcode(security_dir, "sbom-syft-cyclonedx"),
                "report": "sbom.spdx.json",
                "format": "spdx-json",
            },
            "license-summary": {
                "report": "license-summary.json",
                "format": "project-json",
                "license_count": len(licenses),
            },
            "python-license-summary": {
                "report": "python-license-summary.json",
                "format": "project-json",
                "license_count": len(python_license_map),
                "package_count": len(
                    {(p["name"], p["version"]) for p in python_packages}
                ),
            },
            "config-scan-trivy": {
                "exit_code": load_exitcode(security_dir, "config-scan-trivy"),
                "report": "config-scan-trivy.sarif",
                "format": "sarif",
            },
            "lint-ruff": {
                "exit_code": load_exitcode(security_dir, "lint-ruff"),
                "report": "lint-ruff.json",
                "format": "ruff-json",
                "findings": len(ruff) if isinstance(ruff, list) else 0,
            },
            "sast-bandit": {
                "exit_code": load_exitcode(security_dir, "sast-bandit"),
                "report": "sast-bandit.json",
                "format": "bandit-json",
                "findings": len(bandit.get("results", []))
                if isinstance(bandit, dict)
                else 0,
            },
        },
    }
    (security_dir / "security-summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n"
    )

    reports = sorted(path for path in security_dir.iterdir() if path.is_file())
    proof_lines = [
        f"security_check_started_at={started_at}",
        f"security_check_finished_at={finished_at}",
        "tools=semgrep/semgrep,zricethezav/gitleaks:latest,ghcr.io/google/osv-scanner:latest,anchore/syft:latest,aquasec/trivy:latest",
        f"git_branch={run_git('rev-parse', '--abbrev-ref', 'HEAD')}",
        f"git_commit={run_git('rev-parse', 'HEAD')}",
        f"git_dirty={'true' if run_git('status', '--porcelain') else 'false'}",
    ]
    for path in reports:
        proof_lines.append(f"{path.name}_sha256={sha256(path)}")
    (security_dir / "security-proof.txt").write_text("\n".join(proof_lines) + "\n")


def build_report(security_dir: Path, output: Path) -> None:
    summary = load_json(security_dir / "security-summary.json", {})
    license_summary = load_json(security_dir / "license-summary.json", {})
    python_license_summary = load_json(security_dir / "python-license-summary.json", {})
    semgrep = load_json(security_dir / "sast-semgrep.gitlab.json", {})
    gitleaks = load_json(security_dir / "secret-detection-gitleaks.sarif", {})
    trivy = load_json(security_dir / "config-scan-trivy.sarif", {})
    osv = load_json(security_dir / "dependency-audit-osv.json", {})
    ruff = load_json(security_dir / "lint-ruff.json", [])
    bandit = load_json(security_dir / "sast-bandit.json", {})
    proof = parse_proof(security_dir / "security-proof.txt")

    checks = summary.get("checks", {})
    component_rows: list[list[Any]] = []
    for name, info in checks.items():
        exit_code = info.get("exit_code", "-")
        status = "INFO"
        if isinstance(exit_code, int):
            # For osv-scanner, exit code 1 means vulnerabilities found (not an error)
            if name == "dependency-audit-osv" and exit_code == 1:
                status = "INFO"
            else:
                status = "PASS" if exit_code == 0 else "FAIL"
        note = ""
        if name == "sast-semgrep":
            note = f"findings: {info.get('findings', 0)}"
        elif name == "dependency-audit-osv":
            note = f"result blocks: {info.get('results', 0)}"
        elif name == "sbom-cyclonedx":
            note = f"components: {info.get('components', 0)}"
        elif name == "license-summary":
            note = f"licenses: {info.get('license_count', 0)}"
        elif name == "python-license-summary":
            note = f"licenses: {info.get('license_count', 0)}, packages: {info.get('package_count', 0)}"
        component_rows.append(
            [
                name,
                status,
                exit_code,
                info.get("format", "-"),
                info.get("report", "-"),
                note,
            ]
        )

    semgrep_rows: list[list[Any]] = []
    for item in semgrep.get("vulnerabilities", []):
        loc = item.get("location", {})
        semgrep_rows.append(
            [
                item.get("category", "-"),
                (item.get("severity") or "UNKNOWN").upper(),
                loc.get("file", "-"),
                loc.get("start_line", "-"),
                (item.get("message") or "")[:220],
            ]
        )

    gitleaks_rows: list[list[Any]] = []
    for item in gitleaks.get("runs", [{}])[0].get("results", []):
        loc = (item.get("locations") or [{}])[0].get("physicalLocation", {})
        gitleaks_rows.append(
            [
                item.get("ruleId", "-"),
                (item.get("level") or "none").upper(),
                loc.get("artifactLocation", {}).get("uri", "-"),
                loc.get("region", {}).get("startLine", "-"),
                (item.get("message") or {}).get("text", "")[:200],
            ]
        )

    trivy_rows: list[list[Any]] = []
    for item in trivy.get("runs", [{}])[0].get("results", []):
        loc = (item.get("locations") or [{}])[0].get("physicalLocation", {})
        trivy_rows.append(
            [
                item.get("ruleId", "-"),
                (item.get("level") or "none").upper(),
                loc.get("artifactLocation", {}).get("uri", "-"),
                loc.get("region", {}).get("startLine", "-"),
                (item.get("message") or {}).get("text", "")[:220],
            ]
        )

    ruff_rows: list[list[Any]] = []
    if isinstance(ruff, list):
        for item in ruff:
            if not isinstance(item, dict):
                continue
            ruff_rows.append(
                [
                    item.get("code", "-"),
                    item.get("message", "")[:200],
                    item.get("filename", "-"),
                    f"{item.get('location', {}).get('row', '-')}:{item.get('location', {}).get('column', '-')}",
                ]
            )

    bandit_rows: list[list[Any]] = []
    if isinstance(bandit, dict):
        for item in bandit.get("results", []):
            if not isinstance(item, dict):
                continue
            bandit_rows.append(
                [
                    item.get("test_id", "-"),
                    (item.get("severity") or "UNKNOWN").upper(),
                    item.get("filename", "-").replace("/src/", ""),
                    item.get("line_number", "-"),
                    (item.get("issue_text") or "")[:200],
                ]
            )

    license_rows: list[list[Any]] = []
    for item in license_summary.get("licenses", []):
        license_id = item.get("license", "unknown")
        packages = item.get("packages", [])
        package_names = ", ".join(
            sorted(
                {
                    f"{p.get('name', 'unknown')}@{p.get('version', '')}".rstrip("@")
                    for p in packages
                }
            )
        )
        license_display = (
            str(license_id).split("\n")[0][:100] if license_id else "unknown"
        )
        license_rows.append([license_display, len(packages), package_names[:300]])

    python_license_rows: list[list[Any]] = []
    for item in python_license_summary.get("licenses", []):
        license_id = item.get("license", "unknown")
        packages = item.get("packages", [])
        package_names = ", ".join(
            sorted(
                {
                    f"{p.get('name', 'unknown')}@{p.get('version', '')}".rstrip("@")
                    for p in packages
                }
            )
        )
        license_display = (
            str(license_id).split("\n")[0][:100] if license_id else "unknown"
        )
        python_license_rows.append(
            [license_display, len(packages), package_names[:300]]
        )

    osv_rows: list[dict[str, Any]] = []
    osv_severity = Counter()
    osv_frontend_critical = 0
    for block in osv.get("results", []):
        source = block.get("source", {})
        source_path = source.get("path") or source.get("name") or "-"
        for pkg in block.get("packages", []):
            p = pkg.get("package", {})
            for vuln in pkg.get("vulnerabilities", []):
                severity = (
                    vuln.get("database_specific", {}).get("severity") or "UNKNOWN"
                ).upper()
                osv_severity[severity] += 1
                row = {
                    "severity": severity,
                    "pkg": p.get("name", "-"),
                    "version": p.get("version", "-"),
                    "id": vuln.get("id", "-"),
                    "summary": (
                        vuln.get("summary") or vuln.get("details") or ""
                    ).replace("\n", " ")[:220],
                    "source": source_path,
                }
                if severity == "CRITICAL" and "/frontend/" in source_path:
                    osv_frontend_critical += 1
                if severity == "CRITICAL" and "/frontend_chat/" in source_path:
                    osv_frontend_critical += 1
                osv_rows.append(row)

    osv_rows.sort(
        key=lambda row: (
            SEVERITY_ORDER.get(row["severity"], 99),
            row["pkg"],
            row["id"],
        )
    )

    osv_all_rows = [
        [r["severity"], r["pkg"], r["version"], r["id"], r["summary"], r["source"]]
        for r in osv_rows
    ]

    total_semgrep = len(semgrep.get("vulnerabilities", []))
    total_gitleaks = len(gitleaks_rows)
    total_trivy = len(trivy_rows)
    total_osv = len(osv_rows)
    total_ruff = len(ruff_rows)
    total_bandit = len(bandit_rows)

    risk_score = (
        sum(WEIGHTS.get(row["severity"], 1) for row in osv_rows)
        + total_gitleaks * 4
        + total_trivy * 2
        + total_bandit * 3
        + total_ruff * 1
    )
    if risk_score >= 250:
        risk_band = "CRITICAL"
    elif risk_score >= 120:
        risk_band = "HIGH"
    elif risk_score >= 50:
        risk_band = "MEDIUM"
    else:
        risk_band = "LOW"

    sev_chips = "".join(
        f"<span class='chip {esc(level.lower())}'>{esc(level)}: {count}</span>"
        for level, count in sorted(
            osv_severity.items(), key=lambda kv: SEVERITY_ORDER.get(kv[0], 99)
        )
    )

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    html_doc = f"""<!doctype html>
<html lang='en'>
<head>
  <meta charset='utf-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1'>
  <title>Security Report</title>
  <style>
    :root {{
      --bg1: #0f172a;
      --bg2: #111827;
      --panel: #ffffff;
      --text: #0b1220;
      --muted: #475569;
      --line: #e2e8f0;
      --critical: #7f1d1d;
      --high: #991b1b;
      --moderate: #92400e;
      --low: #166534;
      --unknown: #334155;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      padding: 22px;
      color: var(--text);
      background:
        radial-gradient(circle at 7% 12%, #1d4ed8 0, transparent 24%),
        radial-gradient(circle at 92% 88%, #0891b2 0, transparent 22%),
        linear-gradient(140deg, var(--bg1), var(--bg2));
      font-family: 'Iosevka', 'Menlo', 'Consolas', monospace;
    }}
    .container {{ max-width: 1220px; margin: 0 auto; display: grid; gap: 14px; }}
    .card {{ background: var(--panel); border: 1px solid var(--line); border-radius: 14px; padding: 14px; overflow-x: auto; box-shadow: 0 12px 30px rgba(15,23,42,.22); }}
    h1 {{ margin: 0 0 8px; font-size: 28px; }}
    h2 {{ margin: 0 0 10px; font-size: 18px; }}
    p {{ color: var(--muted); margin: 4px 0; }}
    .kpis {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 8px; margin-top: 10px; }}
    .kpi {{ border: 1px solid var(--line); border-radius: 10px; background: #f8fafc; padding: 10px; }}
    .kpi .v {{ font-size: 24px; font-weight: 700; margin-top: 4px; }}
    .risk-CRITICAL, .risk-HIGH {{ color: #991b1b; }}
    .risk-MEDIUM {{ color: #92400e; }}
    .risk-LOW {{ color: #166534; }}
    .chips {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }}
    .chip {{ color: #fff; border-radius: 999px; padding: 6px 10px; font-size: 12px; }}
    .critical {{ background: var(--critical); }}
    .high {{ background: var(--high); }}
    .moderate, .medium {{ background: var(--moderate); }}
    .low {{ background: var(--low); }}
    .unknown {{ background: var(--unknown); }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th, td {{ padding: 8px; text-align: left; border-bottom: 1px solid var(--line); vertical-align: top; }}
    th {{ background: #f8fafc; }}
  </style>
</head>
<body>
  <main class='container'>
    <section class='card'>
      <h1>Security Check Report</h1>
      <p>Generated: {esc(generated_at)}</p>
      <p>Check window: {esc(proof.get("security_check_started_at", "-"))} -> {esc(proof.get("security_check_finished_at", "-"))}</p>
      <p>Branch: {esc(proof.get("git_branch", "-"))} | Commit: {esc(proof.get("git_commit", "-"))} | Dirty tree: {esc(proof.get("git_dirty", "-"))}</p>
      <div class='kpis'>
        <div class='kpi'><div>Semgrep findings</div><div class='v'>{total_semgrep}</div></div>
        <div class='kpi'><div>Gitleaks findings</div><div class='v'>{total_gitleaks}</div></div>
        <div class='kpi'><div>Trivy findings</div><div class='v'>{total_trivy}</div></div>
        <div class='kpi'><div>Ruff lint issues</div><div class='v'>{total_ruff}</div></div>
        <div class='kpi'><div>Bandit security findings</div><div class='v'>{total_bandit}</div></div>
        <div class='kpi'><div>OSV vulnerabilities</div><div class='v'>{total_osv}</div></div>
        <div class='kpi'><div>Frontend critical (OSV)</div><div class='v'>{osv_frontend_critical}</div></div>
                <div class='kpi'><div>Python packages (SBOM)</div><div class='v'>{python_license_summary.get("package_count", 0)}</div></div>
        <div class='kpi'><div>Risk score</div><div class='v risk-{risk_band}'>{risk_score} ({risk_band})</div></div>
      </div>
      <div class='chips'>{sev_chips}</div>
    </section>

    <section class='card'>
      <h2>1. Components</h2>
      {table(["Component", "Status", "Exit code", "Format", "Report", "Notes"], component_rows)}
    </section>

    <section class='card'>
      <h2>2. Semgrep SAST</h2>
      {table(["Category", "Severity", "File", "Line", "Message"], semgrep_rows)}
    </section>

    <section class='card'>
      <h2>3. Gitleaks</h2>
      {table(["Rule", "Level", "File", "Line", "Message"], gitleaks_rows)}
    </section>

    <section class='card'>
      <h2>4. Trivy Misconfig</h2>
      {table(["Rule", "Level", "File", "Line", "Message"], trivy_rows)}
    </section>

    <section class='card'>
      <h2>5. Ruff Lint Issues</h2>
      {table(["Code", "Message", "File", "Location"], ruff_rows)}
    </section>

    <section class='card'>
      <h2>6. Bandit Security Issues</h2>
      {table(["Test ID", "Severity", "File", "Line", "Issue"], bandit_rows)}
    </section>

        <section class='card'>
            <h2>7. OSV All Severities</h2>
            <p>Showing all vulnerabilities across Critical, High, Moderate, Low, and Unknown levels.</p>
            {table(["Severity", "Package", "Version", "Advisory", "Summary", "Source"], osv_all_rows)}
        </section>

        <section class='card'>
            <h2>8. Licenses</h2>
            <p>Detected package licenses from CycloneDX SBOM.</p>
            {table(["License", "Package count", "Sample packages"], license_rows)}
        </section>

        <section class='card'>
            <h2>9. Python Dependency Licenses</h2>
            <p>Licenses mapped specifically to Python packages (purl starts with pkg:pypi/).</p>
            {table(["License", "Python package count", "Sample Python packages"], python_license_rows)}
        </section>
  </main>
</body>
</html>
"""

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html_doc)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate HTML security report")
    parser.add_argument(
        "--security-dir",
        default="security",
        help="Path to security reports directory",
    )
    parser.add_argument(
        "--output",
        default="security/security-report.html",
        help="Output HTML file path",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    security_dir = Path(args.security_dir).resolve()
    output = Path(args.output).resolve()
    generate_security_artifacts(security_dir)
    build_report(security_dir, output)
    print(f"Generated report: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env bash
set -euo pipefail

root_dir="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
out_dir="$root_dir/security"

mkdir -p "$out_dir"
rm -f "$out_dir"/*

run_step() {
  local name="$1"
  shift

  echo "==> $name"
  set +e
  "$@"
  local status=$?
  set -e
  echo "$status" > "$out_dir/$name.exitcode"
  if [ "$status" -ne 0 ]; then
    echo "$name exited with status $status; continuing to collect remaining reports"
  fi
}

docker_run() {
  docker run --rm \
    -v "$root_dir:/src" \
    -w /src \
    "$@"
}

run_step "sast-semgrep" \
  docker_run semgrep/semgrep \
    semgrep scan --config=p/python --gitlab-sast \
    --output /src/security/sast-semgrep.gitlab.json vchat

run_step "secret-detection-gitleaks" \
  docker_run zricethezav/gitleaks:latest \
    git /src \
    --report-format sarif \
    --report-path /src/security/secret-detection-gitleaks.sarif \
    --redact \
    --exit-code 0 \
    --no-banner

run_step "dependency-audit-osv" \
  docker_run ghcr.io/google/osv-scanner:latest \
    scan source -r /src \
    --format json \
    --output-file /src/security/dependency-audit-osv.json \
    --experimental-exclude node_modules \
    --experimental-exclude venv \
    --experimental-exclude dist \
    --experimental-exclude security

run_step "sbom-syft-cyclonedx" \
  docker_run anchore/syft:latest \
    dir:/src \
    --exclude "./venv/**" \
    --exclude "./frontend/node_modules/**" \
    --exclude "./frontend_chat/node_modules/**" \
    --exclude "./frontend/dist/**" \
    --exclude "./frontend_chat/dist/**" \
    --exclude "./security/**" \
    --output cyclonedx-json=/src/security/sbom.cyclonedx.json \
    --output spdx-json=/src/security/sbom.spdx.json

run_step "config-scan-trivy" \
  docker_run aquasec/trivy:latest \
    config /src \
    --format sarif \
    --output /src/security/config-scan-trivy.sarif \
    --skip-dirs /src/venv \
    --skip-dirs /src/frontend/node_modules \
    --skip-dirs /src/frontend_chat/node_modules \
    --skip-dirs /src/frontend/dist \
    --skip-dirs /src/frontend_chat/dist \
    --skip-dirs /src/security

python3 "$root_dir/bin/security-report.py" \
    --security-dir "$out_dir" \
    --output "$out_dir/security-report.html"

echo "Security reports written to: security/"

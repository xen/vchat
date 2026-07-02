import subprocess
import sys
from pathlib import Path


EXTERNAL_TEMPLATE_PATHS = [
    Path("docs/asvs_external_evidence/templates/idp_mfa_recovery_otp_request.md"),
    Path("docs/asvs_external_evidence/templates/tls_ingress_service_encryption_request.md"),
    Path("docs/asvs_external_evidence/templates/secret_management_request.md"),
    Path("docs/asvs_external_evidence/templates/logging_siem_request.md"),
    Path("docs/asvs_external_evidence/templates/antivirus_malware_request.md"),
    Path("docs/asvs_external_evidence/templates/risk_acceptance_template.md"),
]


def test_asvs_checklist_final_report_and_external_package_are_synchronized() -> None:
    result = subprocess.run(
        [sys.executable, "bin/asvs-checklist-verify.py"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "rows=253" in result.stdout
    assert "accepted_external=0" in result.stdout
    assert "external_evidence_pending=0" in result.stdout


def test_asvs_strict_external_evidence_mode_passes_when_no_external_rows_remain() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "bin/asvs-checklist-verify.py",
            "--require-external-evidence",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "accepted_external=0" in result.stdout


def test_asvs_external_status_report_can_be_generated(tmp_path: Path) -> None:
    output = tmp_path / "external-status.md"
    result = subprocess.run(
        [
            sys.executable,
            "bin/asvs-checklist-verify.py",
            "--write-external-status",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    report = output.read_text(encoding="utf-8")
    assert "# Статус внешних доказательств ASVS" in report
    assert "Внешних контролей: 0" in report
    assert "Блокирующих внешних доказательств для области кода приложения не осталось" in report


def test_asvs_external_evidence_templates_exist_and_are_linked() -> None:
    readme = Path("docs/asvs_external_evidence/README.md").read_text(encoding="utf-8")
    package = Path("docs/asvs_external_evidence_package.md").read_text(
        encoding="utf-8"
    )

    for template_path in EXTERNAL_TEMPLATE_PATHS:
        assert template_path.is_file(), str(template_path)
        assert template_path.name in readme
        text = template_path.read_text(encoding="utf-8")
        assert "Related ASVS IDs" in text or "Risk Acceptance" in text

    assert "docs/asvs_external_evidence/templates/" in package

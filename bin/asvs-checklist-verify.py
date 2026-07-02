#!/usr/bin/env python3
"""Verify ASVS checklist/report consistency for commission evidence."""

from __future__ import annotations

import re
import sys
import argparse
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CHECKLIST = ROOT / "docs" / "asvs_l1_l2_checklist.md"
FINAL_REPORT = ROOT / "docs" / "11_combined_report_updated.md"
EXTERNAL_EVIDENCE = ROOT / "docs" / "asvs_external_evidence_package.md"
EXTERNAL_MANIFEST = ROOT / "docs" / "asvs_external_evidence_manifest.json"
EXPECTED_TOTAL_ROWS = 253
BLOCKING_STATUSES = {"Missing", "Partial", "TBD"}
ASVS_ID_RE = re.compile(r"`(v5\.0\.0-\d+(?:\.\d+)*)`")
ALLOWED_EXTERNAL_DISPOSITIONS = {
    "pending_external",
    "evidence_attached",
    "risk_accepted",
    "not_applicable_external",
}


@dataclass(frozen=True)
class AsvsRow:
    id: str
    level: str
    description: str
    status: str
    evidence: str


def split_markdown_row(line: str) -> list[str]:
    body = line.strip().strip("|")
    cells: list[str] = []
    current: list[str] = []
    i = 0
    while i < len(body):
        char = body[i]
        if char == "\\" and i + 1 < len(body):
            current.append(char)
            current.append(body[i + 1])
            i += 2
            continue
        if char == "|":
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(char)
        i += 1
    cells.append("".join(current).strip())
    return cells


def parse_asvs_rows(path: Path) -> dict[str, AsvsRow]:
    rows: dict[str, AsvsRow] = {}
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.startswith("| `v5.0.0-"):
            continue
        cells = split_markdown_row(line)
        if len(cells) != 5:
            raise AssertionError(
                f"{path.relative_to(ROOT)}:{lineno}: expected 5 columns, got {len(cells)}"
            )
        row = AsvsRow(
            id=cells[0].strip("`"),
            level=cells[1],
            description=cells[2],
            status=cells[3],
            evidence=cells[4],
        )
        if not row.description:
            raise AssertionError(f"{path.relative_to(ROOT)}:{lineno}: empty description")
        if not row.evidence:
            raise AssertionError(f"{path.relative_to(ROOT)}:{lineno}: empty evidence")
        if row.id in rows:
            raise AssertionError(f"{path.relative_to(ROOT)}:{lineno}: duplicate {row.id}")
        rows[row.id] = row
    return rows


def external_per_row_ids(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    start_marker = "## Per-Row External Checklist"
    end_marker = "## Evidence Matrix"
    if start_marker not in text and "blocking external evidence: 0" in text:
        return set()
    if start_marker not in text or end_marker not in text:
        raise AssertionError("external evidence package is missing required sections")
    section = text.split(start_marker, 1)[1].split(end_marker, 1)[0]
    return set(ASVS_ID_RE.findall(section))


def external_manifest_controls(path: Path) -> dict[str, dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    controls = payload.get("controls")
    if not isinstance(controls, list):
        raise AssertionError("external evidence manifest must contain controls list")
    by_id: dict[str, dict] = {}
    for index, control in enumerate(controls):
        if not isinstance(control, dict):
            raise AssertionError(f"external evidence manifest control #{index} is not an object")
        control_id = control.get("id")
        if not isinstance(control_id, str) or not control_id:
            raise AssertionError(f"external evidence manifest control #{index} has no id")
        if control_id in by_id:
            raise AssertionError(f"external evidence manifest has duplicate {control_id}")
        disposition = control.get("disposition")
        if disposition not in ALLOWED_EXTERNAL_DISPOSITIONS:
            raise AssertionError(
                f"external evidence manifest {control_id} has invalid disposition {disposition!r}"
            )
        required_artifacts = control.get("required_artifacts")
        if not isinstance(required_artifacts, list) or not required_artifacts:
            raise AssertionError(
                f"external evidence manifest {control_id} must list required_artifacts"
            )
        evidence_files = control.get("evidence_files")
        if not isinstance(evidence_files, list):
            raise AssertionError(
                f"external evidence manifest {control_id} must list evidence_files"
            )
        by_id[control_id] = control
    return by_id


def verify_external_manifest(
    accepted_external: set[str],
    *,
    require_external_evidence: bool,
) -> tuple[int, int]:
    manifest_controls = external_manifest_controls(EXTERNAL_MANIFEST)
    manifest_ids = set(manifest_controls)
    if accepted_external != manifest_ids:
        raise AssertionError(
            "external evidence manifest mismatch: "
            f"missing={sorted(accepted_external - manifest_ids)} "
            f"extra={sorted(manifest_ids - accepted_external)}"
        )

    attached = 0
    pending = 0
    for control_id, control in manifest_controls.items():
        evidence_files = control["evidence_files"]
        disposition = control["disposition"]
        if disposition == "pending_external":
            pending += 1
        if evidence_files:
            attached += 1
        for evidence_file in evidence_files:
            if not isinstance(evidence_file, str) or evidence_file.startswith("/"):
                raise AssertionError(
                    f"external evidence manifest {control_id} has invalid evidence path"
                )
            evidence_path = ROOT / evidence_file
            if not evidence_path.is_file():
                raise AssertionError(
                    f"external evidence manifest {control_id} references missing file: "
                    f"{evidence_file}"
                )
        if require_external_evidence and (
            disposition == "pending_external" or not evidence_files
        ):
            raise AssertionError(
                f"external evidence required but {control_id} is not attached/accepted"
            )
    return attached, pending


def markdown_escape(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", "<br>")


def render_external_status_report() -> str:
    manifest_controls = external_manifest_controls(EXTERNAL_MANIFEST)
    dispositions = Counter(
        control["disposition"] for control in manifest_controls.values()
    )
    attached = sum(
        1 for control in manifest_controls.values() if control["evidence_files"]
    )
    lines = [
        "# ASVS External Evidence Status",
        "",
        "Generated from `docs/asvs_external_evidence_manifest.json`.",
        "Regenerate with `venv/bin/python bin/asvs-checklist-verify.py --write-external-status docs/asvs_external_evidence_status.md`.",
        "",
        "## Summary",
        "",
        f"- External controls: {len(manifest_controls)}",
        f"- Controls with attached evidence files: {attached}",
    ]
    if not manifest_controls:
        lines.append("- No blocking external evidence controls remain for application-code scope.")
    for disposition, count in sorted(dispositions.items()):
        lines.append(f"- `{disposition}`: {count}")
    lines.extend(
        [
            "",
            "Strict close command:",
            "",
            "```bash",
            "venv/bin/python bin/asvs-checklist-verify.py --require-external-evidence",
            "```",
            "",
            "## Controls",
            "",
            "| ASVS ID | Owner | Area | Disposition | Required artifacts | Evidence files |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for control_id in sorted(manifest_controls):
        control = manifest_controls[control_id]
        required_artifacts = "<br>".join(
            f"- {markdown_escape(str(item))}" for item in control["required_artifacts"]
        )
        evidence_files = "<br>".join(
            f"- `{markdown_escape(str(item))}`" for item in control["evidence_files"]
        )
        if not evidence_files:
            evidence_files = "_none attached_"
        lines.append(
            "| "
            + " | ".join(
                [
                    f"`{control_id}`",
                    markdown_escape(str(control["owner"])),
                    markdown_escape(str(control["area"])),
                    f"`{control['disposition']}`",
                    required_artifacts,
                    evidence_files,
                ]
            )
            + " |"
        )
    if not manifest_controls:
        lines.append(
            "| _none_ | _none_ | _none_ | _none_ | "
            "No blocking external evidence controls remain for application-code scope. | _none_ |"
        )
    lines.append("")
    return "\n".join(lines)


def verify(*, require_external_evidence: bool = False) -> str:
    checklist_rows = parse_asvs_rows(CHECKLIST)
    final_rows = parse_asvs_rows(FINAL_REPORT)
    if len(checklist_rows) != EXPECTED_TOTAL_ROWS:
        raise AssertionError(
            f"{CHECKLIST.relative_to(ROOT)}: expected {EXPECTED_TOTAL_ROWS} rows, "
            f"got {len(checklist_rows)}"
        )
    if len(final_rows) != EXPECTED_TOTAL_ROWS:
        raise AssertionError(
            f"{FINAL_REPORT.relative_to(ROOT)}: expected {EXPECTED_TOTAL_ROWS} rows, "
            f"got {len(final_rows)}"
        )
    if checklist_rows != final_rows:
        missing = sorted(set(checklist_rows) - set(final_rows))
        extra = sorted(set(final_rows) - set(checklist_rows))
        changed = sorted(
            row_id
            for row_id in set(checklist_rows) & set(final_rows)
            if checklist_rows[row_id] != final_rows[row_id]
        )
        raise AssertionError(
            "final report ASVS rows are not synchronized with checklist: "
            f"missing={missing[:5]} extra={extra[:5]} changed={changed[:5]}"
        )

    statuses = Counter(row.status for row in checklist_rows.values())
    blockers = {
        status: count for status, count in statuses.items() if status in BLOCKING_STATUSES
    }
    if blockers:
        raise AssertionError(f"blocking ASVS statuses remain: {blockers}")

    accepted_external = {
        row.id for row in checklist_rows.values() if row.status == "Accepted external"
    }
    external_ids = external_per_row_ids(EXTERNAL_EVIDENCE)
    if accepted_external != external_ids:
        raise AssertionError(
            "external evidence package mismatch: "
            f"missing={sorted(accepted_external - external_ids)} "
            f"extra={sorted(external_ids - accepted_external)}"
        )
    attached_external, pending_external = verify_external_manifest(
        accepted_external,
        require_external_evidence=require_external_evidence,
    )

    return (
        "ASVS docs verified: "
        f"rows={len(checklist_rows)}, "
        f"statuses={dict(sorted(statuses.items()))}, "
        f"accepted_external={len(accepted_external)}, "
        f"external_evidence_attached={attached_external}, "
        f"external_evidence_pending={pending_external}"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify ASVS checklist/report/evidence consistency"
    )
    parser.add_argument(
        "--require-external-evidence",
        action="store_true",
        help=(
            "Fail unless every Accepted external row has non-pending disposition "
            "and existing evidence_files in docs/asvs_external_evidence_manifest.json"
        ),
    )
    parser.add_argument(
        "--write-external-status",
        metavar="PATH",
        help="Write Markdown status report generated from external evidence manifest",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        print(verify(require_external_evidence=args.require_external_evidence))
        if args.write_external_status:
            output = Path(args.write_external_status)
            if not output.is_absolute():
                output = ROOT / output
            output.write_text(render_external_status_report(), encoding="utf-8")
            try:
                display_path = output.relative_to(ROOT)
            except ValueError:
                display_path = output
            print(f"ASVS external evidence status written to: {display_path}")
    except AssertionError as exc:
        print(f"ASVS docs verification failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

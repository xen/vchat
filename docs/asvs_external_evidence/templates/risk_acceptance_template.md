# ASVS External Risk Acceptance Template

Use this only when the responsible external owner cannot provide implementation
evidence but formally accepts the risk. A signed risk acceptance is not the same
as repository-side `Covered`; keep the corresponding ASVS row as
`Accepted external` and set manifest `disposition` to `risk_accepted`.

## Risk Acceptance

```text
ASVS ID(s):
Control area:
External owner:
Reason evidence/control is unavailable:
Compensating controls:
Risk impact:
Risk likelihood:
Expiration / review date:
Approver name:
Approver role:
Approval date:
Signature / ticket / document link:
```

## Manifest Update

Store the signed file under `docs/asvs_external_evidence/`, add its path to
`evidence_files`, and set:

```json
"disposition": "risk_accepted"
```

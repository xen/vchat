# ASVS External Evidence Request: AD/IdP MFA, Recovery, OTP/OOB

Owner: AD/IdP owner, Service Desk owner

Related ASVS IDs:

- `v5.0.0-6.3.3`
- `v5.0.0-6.4.3`
- `v5.0.0-6.4.4`
- `v5.0.0-6.5.1`
- `v5.0.0-6.5.2`
- `v5.0.0-6.5.3`
- `v5.0.0-6.5.4`
- `v5.0.0-6.5.5`
- `v5.0.0-6.6.1`
- `v5.0.0-6.6.2`
- `v5.0.0-6.6.3`
- `v5.0.0-6.8.4`

## Required Attachments

- MFA enforcement export for the vchat user/admin group.
- Exception and breakglass process.
- Production statement proving local `auth_basic_enabled` is disabled or
  protected by MFA in front of the application.
- Forgotten-password reset procedure proving MFA is not bypassed.
- MFA-factor recovery procedure with identity proofing level and audit trail.
- OTP/TOTP/OOB configuration proving one-time use, replay protection, lifetime,
  seed/code generation, storage protection, entropy, request binding, and
  brute-force/rate-limit controls.
- PSTN/SMS policy: disabled, or validated phone plus stronger alternative and
  risk disclosure.
- Authentication strength/recentness policy for vchat users, or written
  fallback that explicitly assumes the minimum authentication strength.

## Owner Statement

Fill this section when the evidence is attached.

```text
Owner:
System:
Evidence export date:
Covered vchat group / application:
MFA enforced: yes/no
Local basic auth disabled or protected by MFA: yes/no
Password reset preserves MFA: yes/no
MFA factor recovery identity proofing level:
OTP/TOTP/OOB mechanisms used:
SMS/PSTN used: yes/no
Exceptions / risk acceptance:
```

## Manifest Update

Store files under `docs/asvs_external_evidence/`, then add their paths to
`docs/asvs_external_evidence_manifest.json` for the affected ASVS IDs and change
`disposition` to `evidence_attached`, `not_applicable_external`, or
`risk_accepted`.

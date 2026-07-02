# ASVS External Evidence Attachments

Кладите сюда необязательные deployment assurance артефакты от внешних
владельцев: AD/IdP, Ingress/TLS, Kubernetes secret management, SOC/SIEM/logging
и antivirus/malware scanning. В текущем application-code ASVS scope эти пункты
классифицированы как `N/A`, а не как блокирующие `Accepted external`.

Шаблоны запросов к владельцам лежат в `templates/`:

- `templates/idp_mfa_recovery_otp_request.md`
- `templates/tls_ingress_service_encryption_request.md`
- `templates/secret_management_request.md`
- `templates/logging_siem_request.md`
- `templates/antivirus_malware_request.md`
- `templates/risk_acceptance_template.md`

Рекомендуемый формат имени:

```text
<asvs-id>__<short-owner-or-topic>.<ext>
```

Примеры:

```text
v5.0.0-6.3.3__idp-mfa-policy.pdf
v5.0.0-12.1.1__public-tls-scan.txt
v5.0.0-16.4.3__siem-forwarding-config.yaml
```

Если future assessment scope снова потребует external evidence workflow,
укажите относительный путь в `docs/asvs_external_evidence_manifest.json` в поле
`evidence_files` нужного пункта и смените `disposition` с `pending_external`
на:

- `evidence_attached`, если артефакт доказывает выполнение;
- `risk_accepted`, если приложено подписанное принятие риска;
- `not_applicable_external`, если внешний владелец письменно подтвердил, что
  механизм не используется в production-контуре.

Финальная строгая проверка:

```bash
venv/bin/python bin/asvs-checklist-verify.py --require-external-evidence
```

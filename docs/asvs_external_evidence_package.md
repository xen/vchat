# ASVS Deployment Recommendations and External-Scope Notes

Документ дополняет `docs/asvs_l1_l2_checklist.md` для пунктов, которые были
разобраны как внешние по отношению к application-code scope. По решению
классификации эти пункты не являются блокирующими `Accepted external`: они
помечены в ASVS-чеклисте как `N/A`, потому что относятся к AD/IdP, Ingress/TLS,
platform/service mesh, secret management, SOC/SIEM или антивирусному контуру, а
не к коду vchat.

## Summary

- ASVS rows currently requiring blocking external evidence: 0.
- Application/repository gaps remaining: 0 `Missing`, 0 `Partial`,
  0 `Accepted external`.
- Deployment recommendations remain documented here so the commission can see
  where the boundary lies between vchat code and customer infrastructure.
- Repository-side synchronization is verified by:

```bash
venv/bin/python bin/asvs-checklist-verify.py
```

## Classification Rules

- `N/A` means the control is outside vchat application-code scope for this
  assessment.
- If a deployment owner wants to provide supporting evidence, store it under
  `docs/asvs_external_evidence/`; it is useful as deployment assurance but is
  not required to close the application-code ASVS checklist.
- If future scope expands to include production infrastructure audit, move the
  relevant rows from `N/A` back into an external-evidence workflow and populate
  `docs/asvs_external_evidence_manifest.json`.

## Deployment Recommendation Matrix

| ASVS IDs | Area | Application-code classification | Optional deployment recommendation |
| --- | --- | --- | --- |
| `v5.0.0-5.4.3` | Antivirus / malware scanning | `N/A`: vchat does not execute untrusted downloaded files in a conventional runtime environment; downloaded content is parsed as data by libraries and constrained by source/type/size controls. | If customer policy requires malware scanning, use perimeter, object-storage, gateway, or endpoint-protection scanning. |
| `v5.0.0-6.3.3` | MFA enforcement | `N/A`: MFA is not implemented or owned by vchat application code; authentication strength belongs to customer AD/IdP or another external access layer. | Enforce MFA in AD/IdP for vchat users/admin group when production policy requires it. |
| `v5.0.0-6.4.3`, `v5.0.0-6.4.4` | Password and MFA-factor recovery | `N/A`: forgotten-password and MFA-factor recovery are not implemented in vchat and belong to AD/IdP/service-desk processes. | Keep recovery procedures in customer IAM/service-desk documentation. |
| `v5.0.0-6.5.1`-`v5.0.0-6.5.5`, `v5.0.0-6.6.1`-`v5.0.0-6.6.3` | OTP/TOTP/OOB lifecycle | `N/A`: vchat does not implement lookup secrets, OOB codes, TOTP seeds, PSTN/SMS OTP, or OOB authentication. | If AD/IdP uses these mechanisms, manage one-time use, entropy, lifetime, binding, and brute-force controls in IdP policy. |
| `v5.0.0-6.8.4` | IdP authentication strength/recentness | `N/A`: vchat uses LDAP bind/local auth and does not consume OIDC/SAML `acr`, `amr`, or `auth_time` claims. | If production needs auth-strength/recentness assertions, enforce and document them at the IdP/access-proxy layer. |
| `v5.0.0-12.1.1`, `v5.0.0-12.1.2`, `v5.0.0-12.2.1`, `v5.0.0-12.2.2` | Public TLS / ingress | `N/A`: TLS protocol, ciphers, HTTPS fallback behavior, and public certificate chain are ingress/load-balancer responsibilities. | Keep TLS 1.2/1.3, recommended ciphers, trusted certificates, HTTPS-only exposure, and HSTS-compatible deployment. |
| `v5.0.0-12.3.1`-`v5.0.0-12.3.4` | Service-to-service encryption | `N/A`: DB/Redis/monitoring/registry/ingress-to-pod/service-mesh encryption is platform/deployment scope. | Treat service-to-service TLS/mTLS, CA trust, and internal HTTP encryption as deployment hardening recommendations. |
| `v5.0.0-13.3.1`, `v5.0.0-13.3.2` | Secret management | `N/A`: secret backend and Kubernetes/platform RBAC are deployment responsibilities; application code reads configuration and rejects default production secrets. | Use Kubernetes Secret, External Secrets, Vault, Sealed Secrets, or equivalent with least-privilege RBAC and lifecycle policy. |
| `v5.0.0-16.4.2`, `v5.0.0-16.4.3` | Log protection and SIEM forwarding | `N/A`: vchat emits sanitized structured logs and `AdminEvent`; log storage immutability, access control, and forwarding are SOC/SIEM/logging-platform scope. | Configure collector, retention, immutability/write protection, reader RBAC, SIEM forwarding, and alert/escalation routing in the platform. |

## Optional Attachment Commands

These commands are examples for deployment assurance. They are not required for
application-code ASVS closure unless the assessment scope expands to production
infrastructure.

```bash
# Verify that ASVS checklist and final report are aligned
venv/bin/python bin/asvs-checklist-verify.py

# Regenerate readable external/deployment status from the manifest
venv/bin/python bin/asvs-checklist-verify.py --write-external-status docs/asvs_external_evidence_status.md

# Kubernetes ingress, services, network policy and secret references
kubectl -n vchat get ingress,svc,networkpolicy,deploy,sa,role,rolebinding -o yaml

# Public TLS scan from an approved workstation
testssl.sh --fast --warnings batch https://<production-vchat-host>/

# Confirm public metrics route does not expose Prometheus text
curl -i https://<production-vchat-host>/metrics

# Confirm application pods receive secrets through Kubernetes secret references
kubectl -n vchat get deploy vchat-web vchat-celery vchat-embedder -o yaml
kubectl -n vchat auth can-i get secrets --as=system:serviceaccount:vchat:<service-account-name>

# Confirm log collector / SIEM resources in the cluster, if Kubernetes-managed
kubectl get daemonset,statefulset,deploy -A | rg -i 'fluent|vector|promtail|loki|elastic|opensearch|siem'
```

## Commission Use

During defense, use this document to explain why the listed rows are `N/A` for
the vchat application-code ASVS checklist. If the commission explicitly expands
the assessment to production infrastructure, use the templates under
`docs/asvs_external_evidence/templates/` to request deployment evidence from the
responsible owners.

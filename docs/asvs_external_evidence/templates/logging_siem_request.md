# ASVS External Evidence Request: Logging, SIEM, Log Protection

Owner: SOC/SIEM / logging platform owner

Related ASVS IDs:

- `v5.0.0-16.4.2`
- `v5.0.0-16.4.3`

## Required Attachments

- Log collector configuration showing vchat application logs are collected.
- Destination SIEM/log processor evidence.
- Retention policy.
- Immutability, write-protection, or access-control evidence proving logs cannot
  be modified by ordinary application/runtime operators.
- Reader/admin RBAC evidence.
- Alert/escalation routing for security events.

## Suggested Commands

```bash
kubectl get daemonset,statefulset,deploy -A | rg -i 'fluent|vector|promtail|loki|elastic|opensearch|siem'
kubectl get clusterrole,clusterrolebinding,role,rolebinding -A -o yaml
```

Use SIEM-native exports/screenshots if the collector is managed outside
Kubernetes.

## Owner Statement

```text
Owner:
Log collector:
Destination SIEM/log store:
Retention:
Immutability/write protection:
Reader/admin roles:
Alert/escalation routing:
Exceptions / risk acceptance:
```
